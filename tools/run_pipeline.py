#!/usr/bin/env python3
"""run_pipeline.py -- 用新的「邏輯推導」流程產生 HxNames.lst。

    python tools/run_pipeline.py --game lllj --game-dir "D:\\Game" \
        --exe "D:\\Game\\game.exe" --out work/HxNames.lllj.lst

給了 ``--exe`` 但沒有 ``--drip`` 時，會先自動產生 ``work/drip_program.json``
（等於先跑 ``python run.py key``），再開始推導。其餘參數說明見
``lib/hxlib/pipeline.py``。
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'lib' / 'hxlib'))


def _load_run():
    spec = importlib.util.spec_from_file_location('hxv4_run', ROOT / 'run.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _prepare_args(argv):
    """把 ``--exe`` 轉成 pipeline 需要的 ``--drip``（必要時先產生 drip）。"""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument('--exe', default=None)
    pre.add_argument('--game-dir', default=None)
    pre.add_argument('--work', default=None)
    pre.add_argument('--drip', default=None)
    ns, _ = pre.parse_known_args(argv)

    cleaned = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--exe':
            i += 2
            continue
        if a.startswith('--exe='):
            i += 1
            continue
        cleaned.append(a)
        i += 1

    has_drip = ns.drip is not None or any(a.startswith('--drip=') for a in cleaned)
    if has_drip:
        return cleaned
    if not ns.exe:
        raise SystemExit('run_pipeline.py: 需要 --drip，或提供 --exe 讓程式自動產生')
    work = Path(ns.work) if ns.work else (ROOT / 'work')
    work.mkdir(parents=True, exist_ok=True)
    exe = Path(ns.exe)
    game = Path(ns.game_dir) if ns.game_dir else exe.parent
    run = _load_run()
    drip = run.step_key(exe, work, work / 'drip_program.json', game)
    cleaned += ['--drip', str(drip)]
    return cleaned


def _run() -> int:
    import vendor  # noqa: E402
    vendor.bootstrap(verbose=True, need_pycryptodome=True, need_cxdec=True)
    from hxlib.pipeline import main  # noqa: E402
    return main(_prepare_args(sys.argv[1:]))


if __name__ == '__main__':
    raise SystemExit(_run())
