"""verify.py -- hash 表查詢／候選驗證。

遊戲的 Hxv4 對照表 key 是 ``BLAKE2s(utf16le(name) + utf16le("xp3hnp"))``，
只有「候選名算出來的 digest 真的在表裡」才會被接受。這一步是整個流程唯一的
真值來源，所以獨立成一個模組：

* ``HashTable`` 持有所有 entry 的 digest（32 bytes）與 provenance（archive/index）。
* ``Verifier`` 負責把大量候選名平行 hash 掉、回傳命中（digest, name）並去重。
* ``write_lst`` 產生 GARbro 的 ``HxNames.lst``。
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

MEDIA = 'xp3hnp'
_MEDIA_BYTES = MEDIA.encode('utf-16le')

# multiprocessing worker 共享的 digest 集合（fork/spawn 都靠 initializer 設定）
_WORKER_TABLE: set[bytes] | None = None


def hash_digest(name: str) -> bytes:
    return hashlib.blake2s(name.encode('utf-16le') + _MEDIA_BYTES, digest_size=32).digest()


def hash_hex(name: str) -> str:
    return hash_digest(name).hex().upper()


# --------------------------------------------------------------------- table
@dataclass
class HashTable:
    digests: set[bytes]                       # 32-byte digest
    provenance: dict[str, tuple[str, int]]    # digest hex -> (archive, entry index)
    warning: set[str]                         # XP3 warning entry 的 hash（不算真檔名）

    def __len__(self) -> int:
        return len(self.digests)

    @classmethod
    def from_corpus(cls, corpus) -> 'HashTable':
        digests = {bytes.fromhex(h) for h in corpus.table}
        provenance = {h.upper(): v for h, v in corpus.table.items()}
        # index 明文檔名（startup.tjs 這種）也是真檔名，一併算進候選宇宙
        for h, (name, arc, idx) in getattr(corpus, 'plain', {}).items():
            digests.add(bytes.fromhex(h))
            provenance[h.upper()] = (arc, idx)
        warning = {h.upper() for h in getattr(corpus, 'warning_hashes', set())}
        return cls(digests=digests, provenance=provenance, warning=warning)

    @classmethod
    def from_json(cls, path: str | Path) -> 'HashTable':
        obj = json.loads(Path(path).read_text(encoding='utf-8'))
        digests = {bytes.fromhex(h) for h in obj}
        provenance = {h.upper(): (v['arc'], v['idx']) for h, v in obj.items()}
        return cls(digests=digests, provenance=provenance, warning=set())

    def contains(self, name: str) -> bool:
        return hash_digest(name) in self.digests


# ------------------------------------------------------------------- verifier
def _verify_chunk(names: list[str]) -> list[tuple[bytes, str]]:
    table = _WORKER_TABLE or set()
    out = []
    for name in names:
        d = hashlib.blake2s(name.encode('utf-16le') + _MEDIA_BYTES, digest_size=32).digest()
        if d in table:
            out.append((d, name))
    return out


def _init_worker(digests: set[bytes]) -> None:
    global _WORKER_TABLE
    _WORKER_TABLE = digests


class Verifier:
    """候選名 → 命中 (digest hex, name)。

    * 單進程可處理數百萬個候選（BLAKE2s 約 1M/s/core）。
    * ``workers>1`` 時用 multiprocessing 分塊；digest 集合用 initializer 傳一次。
    """

    def __init__(self, table: HashTable, *, workers: int | None = None,
                 chunk: int = 100_000, verbose: bool = True):
        self.table = table
        self.workers = workers if workers is not None else max(
            1, min(8, (os.cpu_count() or 4) - 1))
        self.chunk = chunk
        self.verbose = verbose
        self.stats = {'probed': 0, 'hits': 0}
        # 進度回報：callback(probed, hits)
        self.progress_cb = None
        # 單進程模式直接共用模組全域；多進程模式由 initializer 在 worker 設定。
        global _WORKER_TABLE
        _WORKER_TABLE = table.digests

    def log(self, *a) -> None:
        if self.verbose:
            print(*a, flush=True)

    def verify(self, names: Iterable[str]) -> dict[str, str]:
        """回傳 {digest_hex: name}；同一 digest 只保留第一個名字。"""
        found: dict[str, str] = {}
        total = 0
        if self.workers > 1:
            self._ensure_pool()
        buf: list[str] = []
        for name in names:
            if not name:
                continue
            buf.append(name)
            if len(buf) >= self.chunk:
                total += self._verify_block(buf, found)
                if self.progress_cb is not None:
                    try:
                        self.progress_cb(self.stats['probed'] + total,
                                         len(found))
                    except Exception:
                        pass
                buf = []
        if buf:
            total += self._verify_block(buf, found)
        self.stats['probed'] += total
        self.stats['hits'] = len(found)
        if self._pool_obj is not None:
            self.close()
        if self.progress_cb is not None:
            try:
                self.progress_cb(self.stats['probed'], len(found))
            except Exception:
                pass
        return found

    def _verify_block(self, names: list[str], found: dict[str, str]) -> int:
        if self._pool_obj is None:
            for d, name in _verify_chunk(names):
                found.setdefault(d.hex().upper(), name)
            return len(names)
        # 多進程模式的進度在 verify() 結尾回報；單進程則每個 block 回報一次
        blocks = [names[i:i + 50_000] for i in range(0, len(names), 50_000)]
        for hits in self._pool_obj.map(_verify_chunk, blocks, chunksize=1):
            for d, name in hits:
                found.setdefault(d.hex().upper(), name)
        return len(names)

    # ---------------------------------------------------------- pool helpers
    _pool_obj = None

    def _ensure_pool(self) -> None:
        if self._pool_obj is not None:
            return
        self._pool_obj = mp.Pool(self.workers, initializer=_init_worker,
                                 initargs=(self.table.digests,))

    def close(self) -> None:
        if self._pool_obj is not None:
            self._pool_obj.terminate()
            self._pool_obj = None


# ------------------------------------------------------------------- output
def write_lst(hits: dict[str, str], path: str | Path, *, sort: bool = True) -> int:
    """寫 GARbro ``HxNames.lst``（<64-hex>:<name>，UTF-8）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    items = list(hits.items())
    if sort:
        items.sort(key=lambda kv: kv[1].lower())
    with path.open('w', encoding='utf-8', newline='\n') as fh:
        for h, name in items:
            fh.write('%s:%s\n' % (h.upper(), name))
    return len(items)


def read_lst(path: str | Path) -> dict[str, str]:
    """讀 HxNames.lst（容忍 16/64 hex、註解、BOM）。"""
    out: dict[str, str] = {}
    path = Path(path)
    if not path.is_file():
        return out
    with path.open(encoding='utf-8-sig', errors='replace') as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith('#'):
                continue
            if ':' not in s:
                continue
            h, name = s.split(':', 1)
            h = h.strip().upper()
            if len(h) in (16, 32, 64):
                out.setdefault(h, name)
    return out
