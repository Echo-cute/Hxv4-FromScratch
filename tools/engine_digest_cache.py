#!/usr/bin/env python3
"""engine_digest_cache.py -- PackinOne digest 校準器（引擎層，自動化）。

用途
----
PackinOne 的 `chthum_*` 這類 runtime 縮圖名字是
`makeDataPackDigest(octet)` 的 64-bit 結果；把它做成 cache 之後，
pipeline 就能在純 Python、無遊戲的情況下用「模板 + digest」推導名字
（模板／角色名都來自遊戲自己的 TJS/資料檔，不是寫死常數）。

流程（全自動、不玩遊戲）
------------------------
1. 掃 XP3 / 解密 --kind 指定的 corpus entry（預設 png）。
2. 把每個 entry 的內容包成 TJS/ns0 data pack（root = octet）。
3. 產生 startup.tjs：`Plugins.link("PackinOne.dll")` 後對每個 pack
   呼叫 `Scripts.makeDataPackDigest(d,0,void,1)`，把 suffix 印到 console log。
4. 以 `-startup=<script>` 啟動遊戲（批次），解析 log，寫出
   ``work/engine/packinone_digest_<game>.json`` = {entry_hash: suffix}。

用法::

    python tools/engine_digest_cache.py --game lllj \
        --game-dir "C:\\Game" --drip work/lllj1/drip_program.json \
        --work work --batch 200
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'lib' / 'hxlib'))

import vendor  # noqa: E402
vendor.setup()
import xp3_inspect as xi  # noqa: E402

from hxlib.corpus import Corpus  # noqa: E402
from hxlib.formats.tjs import _ByteChecker  # noqa: E402


def w_root_octet(data: bytes, seed: int = 0) -> bytes:
    out = bytearray()
    chk = _ByteChecker(seed)
    out += b'TJS/ns0\x00' + struct.pack('<IHH', seed, 0, 0)
    cb = chk.get_seed(0x03)
    out += struct.pack('<H', (cb << 8) | 0x03)
    out += struct.pack('<I', len(data)) + data
    out += struct.pack('<I', chk.final_check())
    return bytes(out)


def decrypt_entry(corpus: Corpus, h: str) -> bytes | None:
    info = corpus.table.get(h.lower()) or corpus.table.get(h.upper())
    if not info:
        return None
    arc = None
    for a in corpus.archives:
        if a.stem == info[0]:
            arc = a
            break
    if arc is None or info[1] not in arc.by_index:
        return None
    idx = info[1]
    rec = arc.by_index[idx]
    entry = arc.entries[idx]
    st = xi.FilterRuntimeState(
        corpus.drip.build_filter_state(rec.key, arc.descriptor.flags & 1))
    return xi.extract_entry(arc.blob, entry, filter_name='recovered',
                            filter_state=st).data


def make_script(paths: list[str], out_path: Path) -> None:
    lines = [
        'Debug.notice("EDIGEST_BEGIN");',
        'try { Plugins.link("PackinOne.dll"); '
        'Debug.notice("EDIGEST_LINK_OK"); } catch(e) { '
        'Debug.notice("EDIGEST_LINK_ERR "+e); }',
        'var files = [',
    ]
    for p in paths:
        lines.append('  "%s",' % p.replace('\\', '/'))
    # 去掉最後一項的逗號，改成合法陣列（TJS 需要 `];`）
    if lines[-1].strip().endswith(','):
        lines[-1] = lines[-1].rstrip().rstrip(',')
    lines += ['];']
    lines += [
        'var n = 0;',
        'for (var i = 0; i < files.count; i++) {',
        '  try {',
        '    var d = Scripts.loadDataPack(files[i]);',
        '    var h = Scripts.makeDataPackDigest(d, 0, void, 1);',
        '    Debug.notice("EDIGEST " + i + " " + '
        '("%08x%08x".sprintf(h & 0xffffffff, (h >>> 32) & 0xffffffff)));',
        '    n++;',
        '  } catch(e2) { Debug.notice("EDIGEST_ERR " + i + " " + e2); }',
        '}',
        'Debug.notice("EDIGEST_DONE " + n);',
        'Debug.notice("EDIGEST_SCRIPT_END");',
        'throw "EDIGEST_END";',
    ]
    out_path.write_text('\n'.join(lines), encoding='utf-8', newline='\r\n')


def _read_log(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ''
    if raw.startswith(b'\xff\xfe'):
        return raw[2:].decode('utf-16-le', 'replace')
    if raw.startswith(b'\xfe\xff'):
        return raw[2:].decode('utf-16-be', 'replace')
    return raw.decode('utf-8', 'replace')


def run_game(game_dir: str, script: str, log_path: Path,
             timeout: float) -> list[str]:
    """啟動遊戲跑 --startup 腳本，回傳這次 console log 的所有行。

    遊戲每次啟動會重建 console log（UTF-16LE）；這裡先清空再從頭讀，
    並用 --startup 腳本裡的 EDIGEST_DONE 判定結束（throw 才會 flush）。
    """
    exe = Path(game_dir) / ('tenshi_sz.exe' if 'tenshi' in game_dir.lower()
                            else 'limelight_lj.exe')
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_bytes(b'')
    except OSError:
        pass
    # 保證沒有殘留的遊戲實例（單一實例 mutex 會讓新啟動靜默退出）
    for _ in range(2):
        subprocess.run(['taskkill', '/F', '/IM', exe.name],
                       capture_output=True)
        time.sleep(0.5)
    proc = subprocess.Popen([str(exe), '-startup=' + script.replace('\\', '/')],
                            cwd=game_dir)
    t0 = time.time()
    text = ''
    while True:
        time.sleep(0.4)
        text = _read_log(log_path)
        if 'EDIGEST_DONE' in text:
            break
        if proc.poll() is not None:
            time.sleep(0.5)
            text = _read_log(log_path)
            break
        if time.time() - t0 > timeout:
            break
    try:
        proc.kill()
    except Exception:
        pass
    subprocess.run(['taskkill', '/F', '/IM', exe.name], capture_output=True)
    time.sleep(0.4)
    text = _read_log(log_path) or text
    return text.splitlines()


def main(argv=None) -> int:
    vendor.bootstrap(verbose=True, need_pycryptodome=True, need_cxdec=True)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--game', required=True)
    ap.add_argument('--game-dir', required=True)
    ap.add_argument('--drip', required=True)
    ap.add_argument('--work', default=str(ROOT / 'work'))
    ap.add_argument('--corpus-root', default=None)
    ap.add_argument('--batch', type=int, default=150)
    ap.add_argument('--kinds', default='png')
    ap.add_argument('--timeout', type=float, default=300.0)
    ap.add_argument('--out', default=None)
    args = ap.parse_args(argv)

    def ts(*a):
        print('[%s]' % time.strftime('%H:%M:%S'), *a, flush=True)

    work = Path(args.work)
    corpus_root = Path(args.corpus_root) if args.corpus_root else work / 'corpus'
    out = Path(args.out) if args.out else (
        work / 'engine' / ('packinone_digest_%s.json' % args.game))
    out.parent.mkdir(parents=True, exist_ok=True)
    packs_dir = work / 'engine' / args.game / 'packs'
    packs_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir = work / 'engine' / args.game
    log_path = Path(args.game_dir) / 'savedata' / 'krkr.console.log'

    corpus = Corpus(args.game, args.game_dir, args.drip, corpus_root)
    corpus.verbose = True
    corpus.scan()
    entries = corpus.extract(reuse=True, workers=8)
    kinds = set(k.strip() for k in args.kinds.split(','))
    targets = [h for h, ce in entries.items() if ce.kind in kinds]
    ts('kind %s entries: %d' % (sorted(kinds), len(targets)))

    cache: dict[str, str] = {}
    if out.is_file():
        try:
            cache = {k: v for k, v in json.loads(out.read_text(
                encoding='utf-8')).items() if isinstance(v, str)}
        except Exception:
            cache = {}
    todo = [h for h in targets if h not in cache]
    ts('to calibrate:', len(todo))
    t_all = time.time()
    for start in range(0, len(todo), args.batch):
        chunk = todo[start:start + args.batch]
        packs: list[tuple[str, Path]] = []
        for k, h in enumerate(chunk):
            if k and k % 20 == 0:
                ts('  pack %d/%d（解密並寫入 data pack）' % (k, len(chunk)))
            data = decrypt_entry(corpus, h)
            if not data:
                continue
            p = packs_dir / ('b%05d.pbd' % (start + k))
            p.write_bytes(w_root_octet(data))
            packs.append((h, p))
        if not packs:
            continue
        script = scripts_dir / ('cal_%05d.tjs' % start)
        make_script([str(p) for _, p in packs], script)
        lines = run_game(args.game_dir, str(script), log_path, args.timeout)
        got = 0
        for line in lines:
            m = re.search(r'EDIGEST\s+(\d+)\s+([0-9a-fA-F]{16})', line)
            if not m:
                continue
            idx = int(m.group(1))
            if 0 <= idx < len(packs):
                cache[packs[idx][0]] = m.group(2).lower()
                got += 1
        for _, p in packs:
            try:
                p.unlink()
            except OSError:
                pass
        tmp = out.with_suffix('.tmp')
        tmp.write_text(json.dumps(
            {'_meta': {'engine': 'PackinOne', 'source': 'engine calibrator',
                       'game': args.game},
             **{k: cache[k] for k in sorted(cache)}},
            ensure_ascii=False, indent=0), encoding='utf-8')
        tmp.replace(out)
        nbatch = max(1, (len(todo) + args.batch - 1) // args.batch)
        ts('batch %d/%d: got %d/%d, cache %d, %.0fs'
           % (start // args.batch + 1, nbatch, got, len(packs), len(cache),
              time.time() - t_all))
    ts('wrote %s (%d entries, %.0fs)' % (out, len(cache),
                                         time.time() - t_all))
    corpus.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
