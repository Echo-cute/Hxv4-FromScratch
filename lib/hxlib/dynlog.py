"""dynlog.py -- 讀 KrkrDump 的 log，整理資產並收割明文檔名。

KrkrDump 會在 DLL 所在目錄寫一份 log，內容長這樣：

    Excluded "file://./c/asm/game/voice.xp3>剫"     ← 索引裡的假名
    Included "mik_attention2.ogg"                    ← 引擎手上的真名
    Extract "mik_attention2.ogg"                     ← 實際寫出的路徑

`Excluded …<archive>.xp3> …` 指出這筆來自哪個封包，`Included` 是明文檔名。
本模組提供三件事：

    parse_log(path)                  -> [(archive_stem, real_name, extracted_rel)]
    organize(dump, out, rows)        -> 依 .xp3 分資料夾
    harvest(rows)                    -> {hash: name}，可直接併進 HxNames.lst
"""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

RE_EXCLUDED = re.compile(r'\| (?:Excluded|Included) "file://[./]*(.+?\.xp3)>(.*)"$')
RE_INCLUDED = re.compile(r'\| Included "(.+)"$')
RE_EXTRACT = re.compile(r'\| Extract "(.+)"$')

MEDIA = 'xp3hnp'


def file_hash(name: str, media: str = MEDIA) -> str:
    return hashlib.blake2s(name.encode('utf-16le') + media.encode('utf-16le'),
                           digest_size=32).hexdigest()


def parse_log(path: Path):
    """回傳 [(archive_stem, real_name, extracted_relpath)]"""
    rows = []
    archive = None
    pending = None
    for line in Path(path).read_text(encoding='utf-8', errors='replace').splitlines():
        m = RE_EXCLUDED.search(line)
        if m:
            archive = Path(m.group(1).replace('\\', '/')).stem
            continue
        m = RE_INCLUDED.search(line)
        if m:
            pending = m.group(1).replace('\\', '/')
            continue
        m = RE_EXTRACT.search(line)
        if m:
            rel = m.group(1).replace('\\', '/')
            rows.append((archive or '_unknown', pending or Path(rel).name, rel))
            pending = None
    return rows


def organize(dump: Path, out: Path, rows, move=False) -> tuple[int, int]:
    """把扁平輸出改成 <out>/<archive>/<name>。回傳 (處理, 找不到來源)。"""
    dump = Path(dump)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    done = missing = 0
    seen = set()
    for arc, name, rel in rows:
        src = dump / rel
        if not src.is_file():
            missing += 1
            continue
        safe_arc = re.sub(r'[<>:"/\\|?*]', '_', arc)
        dst = out / safe_arc / re.sub(r'[<>:"|?*]', '_', name)
        if dst in seen:
            i = 1
            while True:
                cand = dst.with_name('%s_%d%s' % (dst.stem, i, dst.suffix))
                if cand not in seen:
                    dst = cand
                    break
                i += 1
        seen.add(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if move:
            shutil.move(str(src), str(dst))
        else:
            shutil.copy2(src, str(dst))
        done += 1
    return done, missing


def harvest(rows, media: str = MEDIA) -> dict:
    """[(archive, name, rel)] -> {hash: name}（去重）"""
    out = {}
    for _arc, name, _rel in rows:
        if not name:
            continue
        h = file_hash(name, media)
        out.setdefault(h, name)
    return out


def merge_into(harvested: dict, base: Path | None, out: Path) -> tuple[int, int]:
    """把抓到的名字併進既有清單。回傳 (新增, 總數)。"""
    merged = {}
    if base and Path(base).is_file():
        for line in Path(base).read_text(encoding='utf-8', errors='replace').splitlines():
            s = line.strip()
            if ':' in s and len(s.split(':', 1)[0]) == 64:
                h, n = s.split(':', 1)
                merged[h.lower()] = n
    added = 0
    for h, n in harvested.items():
        if h not in merged:
            merged[h] = n
            added += 1
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('w', encoding='utf-8') as fh:
        for h, n in sorted(merged.items(), key=lambda kv: kv[1].lower()):
            fh.write('%s:%s\n' % (h.upper(), n))
    return added, len(merged)


def latest_log(search_dirs) -> Path | None:
    best = None
    for d in search_dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for p in d.glob('KrkrDump-*.log'):
            if p.stat().st_size and (best is None or p.stat().st_mtime > best.stat().st_mtime):
                best = p
    return best
