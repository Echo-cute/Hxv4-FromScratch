#!/usr/bin/env python3
"""build_corpus.py -- 一次性把遊戲的所有 .xp3 解密成 work/corpus/<game>/ 快取。

    python tools/build_corpus.py --game tenshi --game-dir "D:\\Game" --drip work/drip_program.json
    python tools/build_corpus.py --game lllj --game-dir "..." --drip ../work/lllj1/drip_program.json

manifest 寫在 ``work/corpus/<game>/manifest.jsonl``；重跑預設 reuse；``--force`` 重算。
雜湊表另存 ``--table-out``（pipeline 內部其實自己會算，這個只是給分析工具用）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'lib' / 'hxlib'))

import vendor  # noqa: E402

from hxlib.corpus import Corpus  # noqa: E402


def main(argv=None) -> int:
    vendor.bootstrap(verbose=True, need_pycryptodome=True, need_cxdec=True)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--game', required=True, help='game id，例如 tenshi / lllj')
    ap.add_argument('--game-dir', required=True)
    ap.add_argument('--drip', required=True)
    ap.add_argument('--root', default=str(ROOT / 'work' / 'corpus'))
    ap.add_argument('--table-out', default=None)
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args(argv)

    corpus = Corpus(args.game, args.game_dir, args.drip, Path(args.root))
    corpus.scan()
    if args.table_out:
        Path(args.table_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.table_out).write_text(json.dumps(
            {h: {'arc': a, 'idx': i} for h, (a, i) in corpus.table.items()},
            ensure_ascii=False), encoding='utf-8')
        print('table ->', args.table_out)
    manifest = corpus.extract(reuse=not args.force)
    by_kind: dict[str, int] = {}
    for ce in manifest.values():
        by_kind[ce.kind] = by_kind.get(ce.kind, 0) + 1
    print('kinds:', by_kind)
    print('hashes:', len(corpus.table), 'warning:', len(corpus.warning_hashes))
    corpus.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
