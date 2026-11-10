#!/usr/bin/env python3
"""dynamic_dump.py -- 開啟遊戲到主頁，用 KrkrDump 傾印引擎實際打開的檔名。

流程（全自動）：
1. 在 ``work/dyn/<game>/`` 放 KrkrDump.dll / KrkrDumpLoader.exe / KrkrDump.json
2. 以 KrkrDumpLoader 啟動遊戲，等視窗出現後自動點擊數次，讓遊戲走過
   ATTENTION／logo 到標題主頁（可調 --clicks/--interval）。
3. 讀 KrkrDump log 與 dump 目錄，收集所有被打開的檔名。
4. 寫出 ``work/dyn/<game>/HxNames.dynamic.lst``（<BLAKE2s>:name），
   並複製一份到 ``work/HxNames.dynamic.<game>.lst`` 供 merge_lists.py 使用。

用法::

    python tools/dynamic_dump.py --game lllj \
        --game-dir "C:\\Game\\lllj"
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
import vendor  # noqa: E402

from hxlib.progress import Reporter  # noqa: E402

SALT = 'xp3hnp'
SALT_BYTES = SALT.encode('utf-16le')

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

RULES = [
    r'file://\\./.+?\\.xp3>(.+?\\..+$)',
    r'archive://./(.+)',
    r'arc://./(.+)',
    r'bres://./(.+)',
]


def name_hash(name: str) -> str:
    return hashlib.blake2s(name.encode('utf-16le') + SALT_BYTES,
                           digest_size=32).digest().hex().upper()


def _enum_windows():
    hwnds = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def cb(hwnd, lparam):
        hwnds.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return hwnds


class _RECT(ctypes.Structure):
    _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                ('right', ctypes.c_long), ('bottom', ctypes.c_long)]


def find_game_window(pid: int):
    """找遊戲主視窗（跳過 IME/隱藏小窗，取可見面積最大者）。"""
    best = (None, '', 0, (0, 0, 0, 0))
    for hwnd in _enum_windows():
        wpid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            continue
        if not user32.IsWindowVisible(hwnd):
            continue
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 2)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = (buf.value or '').strip()
        if title in ('Default IME', 'MSCTFIME UI', 'IME'):
            continue
        rect = _RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w < 200 or h < 150:
            continue
        area = w * h
        if area > best[2]:
            best = (hwnd, title, area, (rect.left, rect.top, w, h))
    hwnd, title, _area, rect = best
    return hwnd, title, rect


def click(hwnd: int, x: int, y: int) -> None:
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.15)
    user32.SetCursorPos(x, y)
    time.sleep(0.1)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def wait_process(name: str, timeout: float) -> int:
    """找遊戲 PID。注意 tasklist 會截斷長檔名（>25 chars），用 CSV + startswith 比對。"""
    import csv
    import io as _io

    t0 = time.time()
    want = name.lower()
    while time.time() - t0 < timeout:
        out = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True,
                             errors='replace').stdout
        for row in csv.reader(_io.StringIO(out)):
            if len(row) < 2:
                continue
            img = row[0].strip().lower()
            if not img.startswith(want):
                continue
            try:
                return int(row[1])
            except ValueError:
                continue
        time.sleep(0.5)
    return 0


def process_alive(pid: int) -> bool:
    import csv as _csv
    import io as _io
    try:
        out = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid,
                              '/FO', 'CSV', '/NH'], capture_output=True,
                             text=True, errors='replace').stdout
    except Exception:
        return False
    for row in _csv.reader(_io.StringIO(out)):
        if len(row) >= 2 and row[1].strip() == str(pid):
            return True
    return False


def kill(name: str | None = None, pid: int | None = None) -> None:
    if pid:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                       capture_output=True)
    elif name:
        subprocess.run(['taskkill', '/F', '/IM', name], capture_output=True)


def collect_names(dump_dir: Path, log_path: Path) -> set[str]:
    names: set[str] = set()
    if dump_dir.is_dir():
        for p in dump_dir.rglob('*'):
            if p.is_file():
                rel = p.relative_to(dump_dir).as_posix()
                if rel:
                    names.add(rel)
    if log_path.is_file():
        text = log_path.read_bytes()
        if text.startswith(b'\xff\xfe'):
            text = text[2:].decode('utf-16-le', 'replace')
        else:
            text = text.decode('utf-8', 'replace')
        for line in text.splitlines():
            m = re.search(r'(?:Included|Extract)\s+"([^"]+)"', line)
            if not m:
                continue
            raw = m.group(1).replace('\\', '/')
            matched = False
            for rule in RULES:
                mm = re.match(rule, raw)
                if mm:
                    names.add(mm.group(1).replace('\\', '/').lstrip('./'))
                    matched = True
                    break
            if not matched and '://' not in raw:
                names.add(raw.lstrip('./'))
    clean = set()
    for n in names:
        n = n.replace('\\', '/').lstrip('./')
        n = n.split('>')[-1]
        if not n or n.endswith('/'):
            continue
        if any(c in n for c in '\r\n\t\x00'):
            continue
        clean.add(n)
    return clean


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--game', required=True)
    ap.add_argument('--game-dir', required=True)
    ap.add_argument('--exe', default=None, help='遊戲執行檔（預設自動找）')
    ap.add_argument('--work', default=str(ROOT / 'work'))
    ap.add_argument('--clicks', type=int, default=6)
    ap.add_argument('--interval', type=float, default=2.5)
    ap.add_argument('--start-wait', type=float, default=40.0)
    ap.add_argument('--settle', type=float, default=5.0)
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--auto', action='store_true',
                   help='自動點過 ATTENTION/logo 到標題，dump 後關閉遊戲（預設）')
    g.add_argument('--manual', action='store_true',
                   help='只開啟遊戲，讓使用者自己操作到關閉；記錄退出前的所有檔名')
    args = ap.parse_args(argv)

    vendor.bootstrap(verbose=True, need_pycryptodome=False,
                     need_cxdec=False, need_krkr=True)
    mode = 'manual' if args.manual else 'auto'
    game_dir = Path(args.game_dir)
    work = Path(args.work)
    dyn_dir = work / 'dyn' / args.game
    dump_dir = dyn_dir / 'dump'
    dyn_dir.mkdir(parents=True, exist_ok=True)
    dump_dir.mkdir(parents=True, exist_ok=True)
    rep = Reporter(dyn_dir / 'dynamic_dump.log', verbose=True)
    log = rep.log
    log('mode: %s（auto=自動到標題；manual=使用者操作到退出）' % mode)
    # 每次 dump 清掉上次的檔案與 log，避免手動模式混到舊資料
    for old in dump_dir.rglob('*'):
        if old.is_file():
            try:
                old.unlink()
            except OSError:
                pass
    for old in dyn_dir.glob('KrkrDump-*.log'):
        try:
            old.unlink()
        except OSError:
            pass
    for f in ('KrkrDump.dll', 'KrkrDumpLoader.exe', 'inject_launch.exe'):
        src = ROOT / 'tools' / 'krkr' / f
        if not src.is_file():
            print('missing', src, file=sys.stderr)
            return 1
        shutil.copy2(src, dyn_dir / f)
    cfg = {
        'loglevel': 2,
        'enableExtract': True,
        'outputDirectory': str(dump_dir),
        'rules': RULES,
        'includeExtensions': [],
        'excludeExtensions': [],
        'decryptSimpleCrypt': True,
    }
    (dyn_dir / 'KrkrDump.json').write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')

    exe = Path(args.exe) if args.exe else None
    if exe is None:
        cands = [p for p in game_dir.glob('*.exe')
                 if 'load' not in p.name.lower()]
        if not cands:
            print('no exe found in', game_dir, file=sys.stderr)
            return 1
        exe = max(cands, key=lambda p: p.stat().st_size)
    exe = exe.resolve()
    log('game exe:', exe)

    kill(exe.name)
    kill('KrkrDumpLoader.exe')
    loader = dyn_dir / 'KrkrDumpLoader.exe'
    inject = dyn_dir / 'inject_launch.exe'
    if inject.is_file():
        # 手動注入（不經 Detours loader），避免某些遊戲 DRM 被 loader 干擾
        log('launch via inject_launch.exe (manual injection)')
        proc = subprocess.Popen([str(inject), str(dyn_dir / 'KrkrDump.dll'),
                                 str(exe)], cwd=str(dyn_dir))
    else:
        log('launch via KrkrDumpLoader.exe')
        proc = subprocess.Popen([str(loader), str(exe)], cwd=str(dyn_dir))
    log('等待遊戲啟動（最多 %ds）...' % int(args.start_wait))
    pid = wait_process(exe.name, args.start_wait)
    if not pid:
        log('game did not start')
        kill(exe.name)
        kill('KrkrDumpLoader.exe')
        return 2
    hwnd, title, rect = None, '', (0, 0, 0, 0)
    t0 = time.time()
    while time.time() - t0 < 25:
        hwnd, title, rect = find_game_window(pid)
        if hwnd:
            break
        time.sleep(0.5)
    log('視窗:', hex(hwnd) if hwnd else None, repr(title), rect)
    if mode == 'manual':
        # 手動模式：不點擊、不主動關閉；等使用者把遊戲關掉
        log('請操作遊戲（到主頁/各畫面）；關閉遊戲後會自動收集這段期間的檔名')
        t0 = time.time()
        last_report = 0.0
        while process_alive(pid):
            now = time.time()
            if now - last_report >= 5:
                last_report = now
                n_dump = sum(1 for _ in dump_dir.rglob('*') if _.is_file())
                rep.progress('manual', int(now - t0),
                             extra='已 dump %d 個檔名；關閉遊戲即結束' % n_dump,
                             force=True)
            time.sleep(1.0)
        log('遊戲已關閉，開始收集檔名')
        time.sleep(args.settle)
    else:
        if hwnd:
            left, top, width, height = rect
            cx, cy = left + width // 2, top + int(height * 0.6)
            for i in range(args.clicks):
                click(hwnd, cx, cy)
                log('click %d/%d（推進到主頁）' % (i + 1, args.clicks))
                time.sleep(args.interval)
        else:
            log('warning: 找不到遊戲視窗，只會收到 boot 檔名')
    if mode == 'auto':
        rep.progress('settle', 0, extra='等待 dump 寫入 %.0fs' % args.settle,
                     force=True)
        time.sleep(args.settle)
        kill(exe.name, pid=pid)
        kill('KrkrDumpLoader.exe')
        time.sleep(1.0)

    logs = sorted(dyn_dir.glob('KrkrDump-*.log'))
    log_path = logs[-1] if logs else dyn_dir / 'KrkrDump.log'
    rep.progress('collect', 0, extra='解析 dump 目錄與 log', force=True)
    names = collect_names(dump_dir, log_path)
    if not names:
        log('no names collected')
        return 3
    out = dyn_dir / 'HxNames.dynamic.lst'
    lines = ['%s:%s' % (name_hash(n), n) for n in sorted(names)]
    out.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    copy_to = work / ('HxNames.dynamic.%s.lst' % args.game)
    shutil.copy2(out, copy_to)
    log('dump names: %d -> %s' % (len(names), out))
    log('copied -> %s' % copy_to)
    rep.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
