#!/usr/bin/env python3
"""merge_lists.py -- 把「靜態推導清單」與「動態 dump 清單」合併成最終 HxNames.lst。

* 所有名字都用 ``BLAKE2s(name)`` 重新算 hash，再對遊戲 Hxv4 table 驗證；
  表上沒有的名字（別款遊戲的殘留）自動丟棄。
* 保留 16-hex 目錄行（若有）。
* 可用 ``--install-garbro`` 直接複製成 ``GARbro/GameData/HxNames.lst``。

用法::

    python tools/merge_lists.py --game lllj \
        --base work/cleantest/FINAL.lllj.lst \
        --dynamic work/HxNames.dynamic.lllj.lst \
        --table work/table_lllj.json --out work/HxNames.lllj.lst \
        --install-garbro "C:\\asm\\clean-test\\garbro-lllj"
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SALT_BYTES = 'xp3hnp'.encode('utf-16le')


def name_hash(name: str) -> str:
    return hashlib.blake2s(name.encode('utf-16le') + SALT_BYTES,
                           digest_size=32).digest().hex().upper()


def read_lines(path: Path) -> list[tuple[str, str]]:
    out = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        s = line.strip()
        if not s or s.startswith('#') or ':' not in s:
            continue
        h, n = s.split(':', 1)
        h = h.strip().upper()
        if len(h) in (16, 64) and n:
            out.append((h, n))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--game', required=True)
    ap.add_argument('--base', nargs='+', required=True)
    ap.add_argument('--dynamic', nargs='*', default=[])
    ap.add_argument('--table', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--install-garbro', default=None)
    args = ap.parse_args(argv)

    def ts(*a):
        import time
        print('[%s]' % time.strftime('%H:%M:%S'), *a, flush=True)

    ts('merge game=%s base=%d dynamic=%d' % (args.game, len(args.base),
                                              len(args.dynamic)))
    table = json.loads(Path(args.table).read_text(encoding='utf-8'))
    if isinstance(table, dict):
        table = table.get('hashes') or list(table)
    table = {h.upper() for h in table}

    merged: dict[str, str] = {}
    dirs: dict[str, str] = {}
    for p in args.base:
        for h, n in read_lines(Path(p)):
            if len(h) == 16:
                dirs.setdefault(h, n)
            elif name_hash(n) == h and h in table:
                merged.setdefault(h, n)
    before = len(merged)
    dyn_added = 0
    for p in args.dynamic:
        for _h, n in read_lines(Path(p)):
            # 動態 dump 的名稱大小寫可能是遊戲畫面顯示的樣子；XP3 entry 一律小寫，
            # 因此同時試原樣、小寫與 casefold。
            for cand in dict.fromkeys((n, n.lower(), n.casefold())):
                hv = name_hash(cand)
                if hv not in table:
                    continue
                if hv not in merged:
                    merged[hv] = cand
                    dyn_added += 1
                break
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    items = list(merged.items()) + list(dirs.items())
    items.sort(key=lambda kv: kv[1].lower())
    with out.open('w', encoding='utf-8', newline='\n') as fh:
        for h, n in items:
            fh.write('%s:%s\n' % (h, n))
    ts('base %d, dynamic +%d, dirs %d -> %s (%d lines)'
       % (before, dyn_added, len(dirs), out, len(items)))
    if args.install_garbro:
        dst = Path(args.install_garbro)
        if not dst.is_dir():
            ts('garbro dir not found: %s' % dst)
        else:
            data = dst / 'GameData'
            data.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out, data / 'HxNames.lst')
            ts('installed -> %s' % (data / 'HxNames.lst'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
