"""corpus.py -- XP3 掃描 + 全量解密快取（新架構的資料層）。

兩件事：

1. ``scan()``：用 mmap 讀每個 ``*.xp3`` 的 index 與 Hxv4 對照表，得到
   ``{BLAKE2s(檔名): (archive, entry_index)}`` 的完整雜湊表。
2. ``extract()``：把所有 entry 解密，**只落地邏輯型內容**（PSB / TJS / PBD /
   text），供 parser 模組（psb.py / tjs.py / textfmt.py）使用；圖片、音訊、
   影片等媒體不落地（檔名不是從內容來的，只有體積）。

解密流程沿用 ``vendor/cxdec-hxv4-static-analysis`` 的 ``xp3_inspect``：
* Hxv4 對照表給每個 entry 一個 key；``FilterRuntimeState`` 用這把 key 還原
  DripValue 串流 XOR。
* 有些 entry 本來就沒上 filter（adler32 已相符），``extract_entry`` 會自動判斷。

型態偵測（只看解密後檔頭，不看假檔名）：

============================  ========================================
``PSB\\0``                    PSB（場景 / UI / uipsd）
``TJS2100\\0``                TJS2 bytecode（DATA/OBJS chunk）
``TJS/ns0``                   舊型 TJS bytecode 容器
``TJS/4s0``                   PBD 圖層清單
``\\xfe\\xfe``                 descramble 過的文字（KAG / CSV / ini…）
UTF-16LE / UTF-8 / Shift-JIS 的文字          text
</summary>
"""
from __future__ import annotations

import json
import mmap
import multiprocessing as mp
import os
import struct
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Iterator

HERE = Path(__file__).resolve().parent
LIB = HERE.parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import vendor  # noqa: E402

vendor.setup()

import xp3_inspect as xi  # noqa: E402

MEDIA_SUFFIX = 'xp3hnp'
_MEDIA_BYTES = MEDIA_SUFFIX.encode('utf-16le')
_MAGIC_XP3 = b'XP3\r\n \n\x1a\x8b\x67\x01'

# 型態代號（manifest / parser 共用）
T_PSB = 'psb'
T_TJS = 'tjs'          # TJS2100
T_TJS_NS = 'tjs_ns'    # TJS/ns0
T_PBD = 'pbd'          # TJS/4s0
T_TEXT = 'text'        # 0xFE 解擾 / UTF-16 / UTF-8 / SJIS 可讀文字
T_OTHER = 'other'      # 未分類二進位（不落地）

# 媒體型態：不落地，但「目標 entry 是什麼型態」是組合副檔名的重要證據。
T_OGG = 'ogg'
T_OPUS = 'opus'
T_PNG = 'png'
T_JPG = 'jpg'
T_TLG = 'tlg'
T_WAV = 'wav'
T_MOVIE = 'movie'
T_FONT = 'font'        # otf / ttf
T_TFT = 'tft'
T_BINARY = 'binary'

LOGIC_KINDS = (T_PSB, T_TJS, T_TJS_NS, T_PBD, T_TEXT)
MEDIA_KINDS = (T_OGG, T_OPUS, T_PNG, T_JPG, T_TLG, T_WAV, T_MOVIE, T_FONT, T_TFT, T_BINARY)

TEXT_MAGICS = (b'\xfe\xfe',)
_HEAD_BYTES = 8192
# 超過這個大小且不是 PSB/TJS 的 entry 視為媒體（PSB 最大的約 11MB）
MAX_LOGIC_SIZE = 24 * 1024 * 1024


def file_hash(name: str) -> str:
    import hashlib
    return hashlib.blake2s(name.encode('utf-16le') + _MEDIA_BYTES,
                          digest_size=32).hexdigest()


def hash_digest(name: str) -> bytes:
    import hashlib
    return hashlib.blake2s(name.encode('utf-16le') + _MEDIA_BYTES,
                           digest_size=32).digest()


# --------------------------------------------------------------- mmap helpers
def _install_mmap_safe_resolve() -> None:
    """上游的 resolve_index_offset 用 bytes.startswith()；mmap 沒有這個方法。"""
    if getattr(xi, '_hxlib_mmap_safe', False):
        return

    def resolve_index_offset(blob):
        if bytes(blob[:len(_MAGIC_XP3)]) != _MAGIC_XP3:
            raise xi.XP3Error('not an XP3 archive')
        header_offset = struct.unpack_from('<Q', blob, 11)[0]
        if header_offset >= len(blob):
            raise xi.XP3Error('index offset outside file')
        if header_offset == 0x17 and len(blob) >= 0x28:
            candidate = struct.unpack_from('<Q', blob, 0x20)[0]
            if 0 < candidate < len(blob):
                return candidate
        return header_offset

    xi.resolve_index_offset = resolve_index_offset
    xi._hxlib_mmap_safe = True


_install_mmap_safe_resolve()


@dataclass
class CorpusEntry:
    """一個 archive entry 的靜態資訊（manifest 一行）。"""

    hash: str
    archive: str
    index: int
    size: int
    kind: str
    path: str = ''          # 快取相對路徑（kind != other 時有值）
    error: str = ''

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def is_fake_name(name: str) -> bool:
    """Hxv4 把受保護 entry 的檔名換成單一漢字（倀 U+5000、瀀 U+7000 …）。

    明文檔名（例如 ``startup.tjs``）不是單一字元，這種 entry 沒有真正的 Hxv4
    record（它只會被下面那個 sentinel record 指到）。
    """
    return len(name) == 1 and ord(name) >= 0x4e00


# 每個 archive 的第 0 個 record 是「sentinel/domain record」：它的 file_hash
# 固定是同一個值（這個值不是任何檔名的 BLAKE2s；同樣的值會出現在所有 archive，
# 而且它指向的 entry 是 XP3 的 warning entry 或明文檔名 entry，例如 startup.tjs）。
# 真正的檔名對照從 record_index 1 之後開始。
SENTINEL_FILE_HASHES = frozenset({
    '2ea4aaec6a09f9d17e2a5a7ac422fb64b6a42195c55cf6772fb30c0fa0120c8d',
})


def classify_bytes(data: bytes) -> str:
    """依解密後內容的檔頭判斷型態（邏輯型 + 媒體型都分）。"""
    if not data:
        return T_BINARY
    if data.startswith(b'PSB\x00'):
        return T_PSB
    if data.startswith(b'TJS2100\x00'):
        return T_TJS
    if data.startswith(b'TJS/ns0'):
        return T_TJS_NS
    if data.startswith(b'TJS/4s0'):
        return T_PBD
    if data.startswith(TEXT_MAGICS[0]):
        return T_TEXT
    if data.startswith(b'OggS'):
        # Ogg 的第 N 個 segment 會寫 '\x01vorbis' / 'OpusHead'
        head = data[:4000]
        if b'OpusHead' in head:
            return T_OPUS
        return T_OGG
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return T_PNG
    if data[:2] == b'\xff\xd8':
        return T_JPG
    if data.startswith(b'TLG5.0\x00') or data.startswith(b'TLG6.0\x00'):
        return T_TLG
    if data.startswith(b'RIFF') and data[8:12] == b'WAVE':
        return T_WAV
    if data.startswith(b'AJPM') or data[:4] == b'\x00\x00\x00\x18ftyp' or \
            data[4:8] == b'ftyp':
        return T_MOVIE
    if data.startswith(b'OTTO') or data.startswith(b'\x00\x01\x00\x00') or \
            data.startswith(b'true') or data.startswith(b'ttcf') or \
            data.startswith(b'wOFF'):
        return T_FONT
    if data.startswith(b'TFT') or data[:4] == b'\x00TFT':
        return T_TFT
    head = data[:_HEAD_BYTES]
    if _looks_like_utf16(head):
        return T_TEXT
    if _looks_like_utf8(head):
        return T_TEXT
    if _looks_like_sjis(head):
        return T_TEXT
    return T_BINARY


def _kind_rank(kind: str) -> int:
    """比較「過濾後 head」與「原始 head」哪個分類比較可信。"""
    if kind in (T_PSB, T_TJS, T_TJS_NS, T_PBD):
        return 100
    if kind in (T_OGG, T_OPUS, T_PNG, T_JPG, T_TLG, T_WAV, T_MOVIE, T_FONT, T_TFT):
        return 80
    if kind == T_TEXT:
        return 50
    return 0


def _looks_like_utf16(head: bytes) -> bool:
    if len(head) < 8:
        return False
    # UTF-16LE 文字：一半以上的 odd byte 是 0（ASCII）或合法非 ASCII 碼位
    sample = head[:2048]
    zeros = sample[1::2].count(0)
    if zeros < len(sample[1::2]) * 0.3:
        return False
    try:
        text = sample.decode('utf-16le', 'replace')
    except Exception:
        return False
    good = sum(1 for ch in text if ch in '\r\n\t' or 0x20 <= ord(ch) < 0xfff0)
    return good > len(text) * 0.9


def _looks_like_utf8(head: bytes) -> bool:
    if len(head) < 16:
        return False
    for enc in ('utf-8', 'utf-8-sig'):
        try:
            text = head.decode(enc)
        except UnicodeDecodeError as ex:
            # 只要求前段可解（尾端可能被截斷）
            if ex.start >= len(head) - 4:
                text = head[:ex.start].decode(enc, 'replace')
            else:
                continue
        if not text:
            continue
        good = sum(1 for ch in text if ch in '\r\n\t' or 0x20 <= ord(ch) < 0xfff0)
        if good > len(text) * 0.92:
            return True
    return False


def _looks_like_sjis(head: bytes) -> bool:
    if len(head) < 16:
        return False
    try:
        text = head.decode('cp932', 'replace')
    except Exception:
        return False
    good = sum(1 for ch in text if ch in '\r\n\t' or 0x20 <= ord(ch) < 0xfff0)
    if good > len(text) * 0.9 and (b'\r\n' in head or b'\n' in head):
        return True
    return False


def _decrypt_head(blob, entry, state, nbytes: int = _HEAD_BYTES) -> bytes:
    """只解密前 nbytes bytes（segment 可能壓縮 → 用 decompressobj 部分解壓）。"""
    import zlib
    parts: list[bytes] = []
    want = nbytes
    logical = 0
    for seg in entry.segments:
        raw = blob[seg.offset: seg.offset + min(seg.archived_size, max(want * 8, 65536))]
        if seg.compressed:
            try:
                raw = zlib.decompressobj().decompress(raw, max(4096, want))
            except zlib.error:
                return b''
        if not raw:
            continue
        buf = bytearray(raw[:want])
        if state is not None:
            state.apply(buf, logical)
        parts.append(bytes(buf))
        want -= len(parts[-1])
        if want <= 0:
            break
    return b''.join(parts)


class Archive:
    """一個已開啟（mmap）的 xp3 與它的 Hxv4 資訊。"""

    def __init__(self, path: Path, drip: 'xi.DripProgram'):
        self.path = path
        self.stem = path.stem
        self._fh = open(path, 'rb')
        self.blob = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        index = xi.load_index(self.blob)[3]
        self.entries = xi.parse_entries(index)
        self.descriptor, self.records = xi.parse_hxv4_table(
            self.blob, index, self.entries, drip)
        self.by_index: dict[int, 'xi.Hxv4Record'] = {}
        self.sentinel_records = []
        for rec in self.records:
            if rec.file_hash.lower() in SENTINEL_FILE_HASHES:
                self.sentinel_records.append(rec)
                continue
            if rec.archive_slot == 0 and rec.xp3_entry_index is not None:
                self.by_index.setdefault(rec.xp3_entry_index, rec)

    def close(self) -> None:
        try:
            self.blob.close()
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 並行解密 worker（ProcessPoolExecutor + spawn）
# ---------------------------------------------------------------------------
_W_CTX: dict[str, str] = {}


def _extract_init(game_dir: str, drip_path: str) -> None:
    _W_CTX['game_dir'] = game_dir
    _W_CTX['drip_path'] = drip_path


def _extract_task(task: tuple[str, list[int], str]) -> tuple[str, list[dict]]:
    """解密一個 archive 的一批 entry；回傳 (archive_stem, rows)。"""
    arc_path, indices, root = task
    drip = xi.DripProgram.load(Path(_W_CTX['drip_path']))
    arc = Archive(Path(arc_path), drip)
    out: list[dict] = []
    states: dict[int, object] = {}
    try:
        for idx in indices:
            rec = arc.by_index.get(idx)
            if rec is None:
                continue
            entry = arc.entries[idx]
            row = {'hash': rec.file_hash.lower(), 'archive': arc.stem,
                   'index': idx, 'size': entry.original_size,
                   'kind': T_BINARY, 'path': '', 'error': ''}
            try:
                if idx == 0 and xi.is_warning_entry(0, entry):
                    row['error'] = 'warning entry'
                else:
                    if idx not in states:
                        try:
                            states[idx] = xi.FilterRuntimeState(
                                drip.build_filter_state(
                                    rec.key, arc.descriptor.flags & 1))
                        except Exception:
                            states[idx] = None
                    st = states[idx]
                    head = _decrypt_head(arc.blob, entry, st)
                    kind = classify_bytes(head)
                    if kind in (T_BINARY, T_TEXT) or not head:
                        k2 = classify_bytes(_decrypt_head(arc.blob, entry, None))
                        if _kind_rank(k2) > _kind_rank(kind):
                            kind = k2
                    row['kind'] = kind
                    if kind in LOGIC_KINDS:
                        data = xi.extract_entry(
                            arc.blob, entry, filter_name='recovered',
                            filter_state=st).data
                        kind = classify_bytes(data)
                        row['kind'] = kind
                        ext = {T_PSB: '.psb', T_TJS: '.tjs',
                               T_TJS_NS: '.tjsns', T_PBD: '.pbd',
                               T_TEXT: '.txt'}.get(kind, '.bin')
                        rel = Path(arc.stem) / ('e%05d%s' % (idx, ext))
                        dst = Path(root) / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        dst.write_bytes(data)
                        row['path'] = str(rel)
                    elif (entry.original_size > MAX_LOGIC_SIZE
                          and kind == T_BINARY):
                        row['error'] = 'media (too large)'
            except Exception as ex:
                row['error'] = '%s: %s' % (type(ex).__name__, ex)
            out.append(row)
    finally:
        arc.close()
    return arc.stem, out


class Corpus:
    """遊戲的 XP3 資料層：掃描 → 雜湊表 → 解密快取 → manifest。"""

    def __init__(self, game_id: str, game_dir: str | Path, drip_path: str | Path,
                 root: str | Path = 'work/corpus'):
        self.game_id = game_id
        self.game_dir = Path(game_dir)
        self.drip_path = Path(drip_path)
        self.drip = xi.DripProgram.load(Path(drip_path))
        self.root = Path(root) / game_id
        self.archives: list[Archive] = []
        self.table: dict[str, tuple[str, int]] = {}   # name-hash -> (archive, idx)
        self.plain: dict[str, tuple[str, str, int]] = {}  # hash -> (真檔名, archive, idx)
        self.warning_hashes: set[str] = set()
        # Hxv4 對照表是「按目錄分組」的：domain_hash(16 hex) 就是原 build 的目錄。
        self.file_domain: dict[str, str] = {}     # file_hash -> domain_hash
        self.domain_files: dict[tuple[str, str], list[str]] = {}  # (arc, dom) -> [file_hash]
        self.domain_order: list[tuple[str, str]] = []            # 表中出現順序
        self.domain_archives: dict[str, set[str]] = {}           # domain -> archives
        self.root_domain: str | None = None
        self._opened: dict[str, Archive] = {}
        self.verbose = True

    # ------------------------------------------------------------- logging
    def log(self, *a) -> None:
        fn = getattr(self, 'log_fn', None)
        if fn is not None:
            fn(*a)
        elif self.verbose:
            print(*a, flush=True)

    # --------------------------------------------------------------- scan
    def scan(self) -> dict[str, tuple[str, int]]:
        """建立完整 hash 表（archive_slot == 0 的 record）。

        另外收集「index 裡本來就是明文檔名」的 entry（Hxv4 只把被保護的 entry
        改成假名 `倀 倁 倂…`；`startup.tjs` 這種是明文）。明文 entry 沒有 Hxv4
        record，但它們的名字直接就是真檔名，屬於「所有名字」的一部分。
        """
        arcs = sorted(self.game_dir.glob('*.xp3'))
        if not arcs:
            raise SystemExit('找不到 *.xp3：%s' % self.game_dir)
        for path in arcs:
            try:
                arc = Archive(path, self.drip)
            except Exception as ex:
                self.log('  skip %s (%s: %s)' % (path.name, type(ex).__name__, ex))
                continue
            self.archives.append(arc)
            n = 0
            seen_dom: set[str] = set()
            for rec in arc.records:
                if rec.file_hash.lower() in SENTINEL_FILE_HASHES:
                    continue
                if rec.archive_slot != 0 or rec.xp3_entry_index is None:
                    continue
                h = rec.file_hash.lower()
                self.table.setdefault(h, (arc.stem, rec.xp3_entry_index))
                dom = rec.domain_hash.lower()
                self.file_domain.setdefault(h, dom)
                self.domain_files.setdefault((arc.stem, dom), []).append(h)
                self.domain_archives.setdefault(dom, set()).add(arc.stem)
                if dom not in seen_dom:
                    seen_dom.add(dom)
                    self.domain_order.append((arc.stem, dom))
                idx = rec.xp3_entry_index
                if idx == 0 and xi.is_warning_entry(0, arc.entries[0]):
                    self.warning_hashes.add(h)
                n += 1
            plain = 0
            for idx, entry in enumerate(arc.entries):
                if idx in arc.by_index:
                    continue
                if idx == 0 and xi.is_warning_entry(0, entry):
                    continue          # XP3 warning entry（不是真檔名）
                if not is_fake_name(entry.name):
                    h = file_hash(entry.name).lower()
                    self.plain.setdefault(h, (entry.name, arc.stem, idx))
                    plain += 1
            self.log('  %-16s entries=%-6d records=%d plain=%d'
                     % (path.name, len(arc.entries), n, plain))
        # root domain：出現在最多 archive、且檔案數最多的 domain（每個 xp3 都有一個）
        if self.domain_archives:
            self.root_domain = max(
                self.domain_archives,
                key=lambda d: (len(self.domain_archives[d]),
                               sum(len(v) for (a, dd), v in self.domain_files.items()
                                   if dd == d)))
        self.log('total hashes: %d + plain-text names: %d = %d (warning: %d), '
                 'domain groups: %d (root=%s)'
                 % (len(self.table), len(self.plain),
                    len(self.table) + len(self.plain), len(self.warning_hashes),
                    len(self.domain_archives), self.root_domain))
        return self.table

    # ------------------------------------------------------------ extract
    def _arc(self, stem: str) -> Archive:
        arc = self._opened.get(stem)
        if arc is not None:
            return arc
        for a in self.archives:
            if a.stem == stem:
                self._opened[stem] = a
                return a
        raise KeyError(stem)

    def _manifest_path(self) -> Path:
        return self.root / 'manifest.jsonl'

    def _fill_plain(self, manifest: dict[str, CorpusEntry]) -> None:
        """把「index 明文 entry」補進 manifest（沒有 Hxv4 record、不套 filter）。"""
        todo = [(h, v) for h, v in self.plain.items() if h not in manifest]
        if not todo:
            return
        self.log('  filling %d plain-text index entries' % len(todo))
        for h, (name, arc_stem, idx) in todo:
            try:
                arc = self._arc(arc_stem)
            except KeyError:
                continue
            entry = arc.entries[idx]
            ce = CorpusEntry(hash=h, archive=arc_stem, index=idx,
                             size=entry.original_size, kind=T_BINARY,
                             error='index plaintext name: ' + name)
            try:
                data = xi.extract_entry(arc.blob, entry, filter_name='none').data
                kind = classify_bytes(data)
                ce.kind = kind
                if kind in LOGIC_KINDS:
                    ext = {T_PSB: '.psb', T_TJS: '.tjs', T_TJS_NS: '.tjsns',
                           T_PBD: '.pbd', T_TEXT: '.txt'}.get(kind, '.bin')
                    rel = Path(arc_stem) / ('e%05d%s' % (idx, ext))
                    (self.root / rel).write_bytes(data)
                    ce.path = str(rel)
            except Exception as ex:
                ce.error = '%s: %s' % (type(ex).__name__, ex)
            manifest[h] = ce
        try:
            tmp = self._manifest_path().with_suffix('.tmp')
            with tmp.open('w', encoding='utf-8') as fh:
                for h2, ce in manifest.items():
                    fh.write(ce.to_json() + '\n')
            tmp.replace(self._manifest_path())
        except OSError:
            pass

    def load_manifest(self) -> dict[str, CorpusEntry] | None:
        path = self._manifest_path()
        if not path.is_file():
            return None
        out: dict[str, CorpusEntry] = {}
        with path.open(encoding='utf-8', errors='replace') as fh:
            for line in fh:
                try:
                    obj = json.loads(line)
                    out[obj['hash']] = CorpusEntry(**obj)
                except Exception:
                    continue
        if not out:
            return None
        return out

    # ------------------------------------------------------ parallel extract
    def _write_manifest(self, entries: dict[str, 'CorpusEntry']) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._manifest_path().with_suffix('.tmp')
        with tmp.open('w', encoding='utf-8') as fh:
            for ce in entries.values():
                fh.write(ce.to_json() + '\n')
        tmp.replace(self._manifest_path())

    def _extract_parallel(self, workers: int,
                          progress_every: int = 4000) -> dict[str, 'CorpusEntry']:
        """以 ProcessPoolExecutor 平行解密（Windows spawn），chunk 大小 4000。"""
        t0 = time.time()
        self.log('extracting corpus (parallel %d workers) -> %s'
                 % (workers, self.root))
        for arc in self.archives:
            (self.root / arc.stem).mkdir(parents=True, exist_ok=True)
        chunk = 4000
        tasks: list[tuple[str, list[int], str]] = []
        for arc in self.archives:
            if not arc.by_index:
                continue
            # 依 entry 大小做 LPT 平衡：大檔先分給目前負載最小的 chunk，
            # 避免最後幾個大檔（voice/*.ogg 等）把尾巴拖長。
            idx_sizes = sorted(
                ((arc.entries[i].original_size, i) for i in arc.by_index),
                reverse=True)
            n_chunks = max(1, (len(idx_sizes) + chunk - 1) // chunk)
            buckets: list[list[int]] = [[] for _ in range(n_chunks)]
            loads = [0] * n_chunks
            for size, idx in idx_sizes:
                j = loads.index(min(loads))
                buckets[j].append(idx)
                loads[j] += size
            for b in buckets:
                tasks.append((str(arc.path), b, str(self.root)))
        results: dict[str, dict[int, dict]] = {}
        total_entries = sum(len(a.by_index) for a in self.archives)
        done = 0
        ctx = mp.get_context('spawn')
        with ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                                 initializer=_extract_init,
                                 initargs=(str(self.game_dir),
                                           str(self.drip_path))) as ex:
            fut_map = {ex.submit(_extract_task, t): t[0] for t in tasks}
            for fut in as_completed(fut_map):
                try:
                    stem, rows = fut.result()
                except Exception as ex:  # 單一 chunk 失敗不應毀掉全部
                    self.log('  [extract] chunk failed: %s: %s'
                             % (type(ex).__name__, ex))
                    continue
                slot = results.setdefault(stem, {})
                for r in rows:
                    slot.setdefault(r['index'], r)
                done += len(rows)
                if progress_every and done % progress_every < chunk:
                    self.log('  解密 %d/%d entries (%.1f%%)  %.0fs'
                             % (done, total_entries,
                                100.0 * done / max(total_entries, 1),
                                time.time() - t0))
        entries: dict[str, 'CorpusEntry'] = {}
        for arc in self.archives:
            rows = results.get(arc.stem, {})
            for idx in sorted(rows):
                r = rows[idx]
                h = r['hash']
                if h in entries:
                    continue
                entries[h] = CorpusEntry(
                    hash=h, archive=r['archive'], index=r['index'],
                    size=r['size'], kind=r['kind'], path=r['path'],
                    error=r['error'])
        # index 明文 entry（例如 startup.tjs）— 數量極少，母進程處理
        by_stem = {a.stem: a for a in self.archives}
        for h, (name, arc_stem, idx) in self.plain.items():
            if h in entries:
                continue
            arc = by_stem.get(arc_stem)
            if arc is None or idx >= len(arc.entries):
                continue
            entry = arc.entries[idx]
            ce = CorpusEntry(hash=h, archive=arc.stem, index=idx,
                             size=entry.original_size, kind=T_BINARY)
            try:
                data = xi.extract_entry(arc.blob, entry, filter_name='none').data
                kind = classify_bytes(data)
                ce.kind = kind
                if kind in LOGIC_KINDS:
                    ext = {T_PSB: '.psb', T_TJS: '.tjs', T_TJS_NS: '.tjsns',
                           T_PBD: '.pbd', T_TEXT: '.txt'}.get(kind, '.bin')
                    rel = Path(arc.stem) / ('e%05d%s' % (idx, ext))
                    (self.root / rel).write_bytes(data)
                    ce.path = str(rel)
            except Exception as ex:
                ce.error = '%s: %s' % (type(ex).__name__, ex)
            entries[h] = ce
        self.entries = entries
        self._write_manifest(entries)
        self.log('corpus: %d entries, %d logic-bearing (%.1fs, %d workers)'
                 % (len(entries),
                    sum(1 for e in entries.values()
                        if e.kind != T_OTHER and e.path),
                    time.time() - t0, workers))
        return entries

    def extract(self, *, reuse: bool = True, workers: int = 1,
                progress_every: int = 2000) -> dict[str, CorpusEntry]:
        """解密所有 entry；只把邏輯型內容落地到 ``<root>/<archive>/e<idx>.<ext>``。"""
        if reuse:
            manifest = self.load_manifest()
            universe = len(self.table) + len(self.plain)
            if manifest and len(manifest) >= universe * 0.9:
                self.log('corpus cache hit: %d entries (%s)'
                         % (len(manifest), self._manifest_path()))
                # 舊版 manifest 可能沒有 index 明文 entry（例如 startup.tjs）：補抽
                self._fill_plain(manifest)
                self.entries = manifest
                return manifest
        if not self.table:
            self.scan()
        if workers and workers > 1:
            return self._extract_parallel(workers, progress_every)
        self.log('extracting corpus -> %s' % self.root)
        for arc in self.archives:
            (self.root / arc.stem).mkdir(parents=True, exist_ok=True)
        entries: dict[str, CorpusEntry] = {}
        t0 = time.time()
        done = 0
        for arc in self.archives:
            if not arc.by_index:
                continue
            # 每個 archive 一次建好所有 FilterRuntimeState
            states = {}
            for idx, rec in arc.by_index.items():
                try:
                    states[idx] = xi.FilterRuntimeState(
                        self.drip.build_filter_state(rec.key, arc.descriptor.flags & 1))
                except Exception:
                    states[idx] = None
            for idx, rec in arc.by_index.items():
                h = rec.file_hash.lower()
                if h in entries:
                    continue
                entry = arc.entries[idx]
                ce = CorpusEntry(hash=h, archive=arc.stem, index=idx,
                                 size=entry.original_size, kind=T_BINARY)
                try:
                    if idx == 0 and xi.is_warning_entry(0, entry):
                        ce.kind = T_BINARY
                        ce.error = 'warning entry'
                        entries[h] = ce
                        continue
                    head_filtered = _decrypt_head(arc.blob, entry, states[idx])
                    kind = classify_bytes(head_filtered)
                    if kind in (T_BINARY, T_TEXT) or not head_filtered:
                        # 有些 entry 本來就沒上 filter；用原始 head 再判一次
                        head_raw = _decrypt_head(arc.blob, entry, None)
                        kind2 = classify_bytes(head_raw)
                        if _kind_rank(kind2) > _kind_rank(kind):
                            kind = kind2
                    ce.kind = kind
                    if kind not in LOGIC_KINDS:
                        # 媒體：不落地，只留型態（組合副檔名時要用）
                        if entry.original_size > MAX_LOGIC_SIZE and kind in (T_BINARY,):
                            ce.error = 'media (too large)'
                        entries[h] = ce
                        continue
                    data = xi.extract_entry(
                        arc.blob, entry, filter_name='recovered',
                        filter_state=states[idx]).data
                    kind = classify_bytes(data)
                    ce.kind = kind
                    ext = {T_PSB: '.psb', T_TJS: '.tjs', T_TJS_NS: '.tjsns',
                           T_PBD: '.pbd', T_TEXT: '.txt'}.get(kind, '.bin')
                    rel = Path(arc.stem) / ('e%05d%s' % (idx, ext))
                    dst = self.root / rel
                    dst.write_bytes(data)
                    ce.path = str(rel)
                except Exception as ex:
                    ce.error = '%s: %s' % (type(ex).__name__, ex)
                entries[h] = ce
                done += 1
                if progress_every and done % progress_every == 0:
                    self.log('  %d entries  %.0fs' % (done, time.time() - t0))
            # index 裡本來就是明文的 entry（沒有 Hxv4 record / filter）
            for h, (name, arc_stem, idx) in self.plain.items():
                if arc_stem != arc.stem or h in entries:
                    continue
                entry = arc.entries[idx]
                ce = CorpusEntry(hash=h, archive=arc.stem, index=idx,
                                 size=entry.original_size, kind=T_BINARY)
                try:
                    data = xi.extract_entry(arc.blob, entry, filter_name='none').data
                    kind = classify_bytes(data)
                    ce.kind = kind
                    if kind in LOGIC_KINDS:
                        ext = {T_PSB: '.psb', T_TJS: '.tjs', T_TJS_NS: '.tjsns',
                               T_PBD: '.pbd', T_TEXT: '.txt'}.get(kind, '.bin')
                        rel = Path(arc.stem) / ('e%05d%s' % (idx, ext))
                        (self.root / rel).write_bytes(data)
                        ce.path = str(rel)
                    entries[h] = ce
                except Exception as ex:
                    ce.error = '%s: %s' % (type(ex).__name__, ex)
                    entries[h] = ce
        self.entries = entries
        # 寫 manifest
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._manifest_path().with_suffix('.tmp')
        with tmp.open('w', encoding='utf-8') as fh:
            for h, ce in entries.items():
                fh.write(ce.to_json() + '\n')
        tmp.replace(self._manifest_path())
        self.log('corpus: %d entries, %d logic-bearing (%.1fs)'
                 % (len(entries), sum(1 for e in entries.values()
                                      if e.kind != T_OTHER and e.path),
                    time.time() - t0))
        return entries

    # ------------------------------------------------------------- access
    def iter_files(self, kinds: Iterable[str] | None = None) -> Iterator[tuple[CorpusEntry, bytes]]:
        """走訪已落地的邏輯型 entry（依 kind 過濾），回傳 (entry, data)。"""
        want = set(kinds) if kinds else None
        for ce in getattr(self, 'entries', {}).values():
            if not ce.path:
                continue
            if want is not None and ce.kind not in want:
                continue
            path = self.root / ce.path
            if not path.is_file():
                continue
            yield ce, path.read_bytes()

    def close(self) -> None:
        for arc in self.archives:
            arc.close()
        self.archives.clear()
        self._opened.clear()


# --------------------------------------------------------------------- main
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='XP3 → hash table + decrypted corpus cache')
    ap.add_argument('--game-dir', required=True)
    ap.add_argument('--drip', required=True)
    ap.add_argument('--name', required=True, help='game id (cache 子目錄)')
    ap.add_argument('--root', default='work/corpus')
    ap.add_argument('--table-out', default=None, help='另存 hash 表 JSON')
    ap.add_argument('--no-reuse', action='store_true')
    args = ap.parse_args(argv)
    corpus = Corpus(args.name, args.game_dir, args.drip, args.root)
    corpus.scan()
    if args.table_out:
        Path(args.table_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.table_out).write_text(json.dumps(
            {h: {'arc': a, 'idx': i} for h, (a, i) in corpus.table.items()},
            ensure_ascii=False), encoding='utf-8')
        print('table ->', args.table_out)
    manifest = corpus.extract(reuse=not args.no_reuse)
    by_kind: dict[str, int] = {}
    for ce in manifest.values():
        by_kind[ce.kind] = by_kind.get(ce.kind, 0) + 1
    print('kinds:', by_kind)
    corpus.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
