#!/usr/bin/env python3
"""run.py -- 從零開始，把一款「Hxv4 加密的 KiriKiri 遊戲」變成 GARbro 能正常列出檔名。

    1. key     取得 drip_program.json（Hxv4 key/nonce + DripValue VM 的 128 條 lane）
    2. names   取得 HxNames.lst（檔名雜湊 → 真實檔名）
    3. install 把插件 DLL + 上面兩份資料放進 GARbro

產生名稱表時只要給遊戲資料夾和遊戲執行檔，程式會先自動產生
``drip_program.json``，再開始推導。

「取得」= 先看 `userdata\\` 有沒有你自己放的檔案；有就用，沒有（或驗證失敗）
才由程式自己算。所以任何人都可以：

    * 完全從零跑（預設）
    * 只放金鑰（別處拿到的 drip_program.json）
    * 只放清單（社群維護的 HxNames.lst）
    * 兩個都放（最快）

用法
----
    python run.py all  --exe "D:\\Game\\game.exe" --game "D:\\Game" --garbro "C:\\GARbro"

    python run.py key     --exe  "D:\\Game\\game.exe" --out work/drip_program.json
    python run.py names   --game "D:\\Game" --exe "D:\\Game\\game.exe" --out work/HxNames.lst
    python run.py names   --game "D:\\Game" --drip work/drip_program.json --out work/HxNames.lst
    python run.py install --garbro "C:\\GARbro" --drip work/drip_program.json --names work/HxNames.lst
    python run.py check   --game "D:\\Game"          # 只檢查 userdata 能不能用
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIB = ROOT / 'lib'
USERDATA = ROOT / 'userdata'
sys.path.insert(0, str(LIB))

# 訊息含中文；在非中文 Windows 上預設 cp1252 會 UnicodeEncodeError 直接中斷。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

_VENDOR_READY = False


def ensure_vendor():
    """cxdec-hxv4-static-analysis 沒有授權條款，本專案不隨附，第一次用到時才下載。"""
    global _VENDOR_READY
    if not _VENDOR_READY:
        import vendor
        vendor.setup ()
        _VENDOR_READY = True


# --------------------------------------------------------------------- utils
def ensure_dotnet():
    """把 dotnet 放進 PATH，`ensure_filter_manager_derive` 會直接呼叫 `dotnet build`。"""
    if shutil.which('dotnet'):
        return True
    for cand in (r'C:\Program Files (x86)\dotnet', r'C:\Program Files\dotnet'):
        exe = Path(cand) / 'dotnet.exe'
        if exe.exists():
            os.environ['PATH'] = cand + os.pathsep + os.environ.get('PATH', '')
            return True
    return False


def log(*a):
    print(*a, flush=True)


def user_key() -> Path:
    return USERDATA / 'drip_program.json'


def user_names() -> Path:
    return USERDATA / 'HxNames.lst'


# ------------------------------------------------------------------ validate
def validate_key(drip: Path, game: Path) -> str:
    """用這份金鑰試開第一個 xp3；成功回傳 ''，失敗回傳原因。"""
    try:
        ensure_vendor ()
        import xp3_inspect as xi
        arc = sorted(Path(game).glob('*.xp3'))[0]
        blob = arc.read_bytes()
        prog = xi.DripProgram.load(drip)
        idx = xi.load_index(blob)[3]
        entries = xi.parse_entries(idx)
        xi.parse_hxv4_table(blob, idx, entries, prog)
        return ''
    except Exception as ex:
        return '%s: %s' % (type(ex).__name__, ex)


def validate_names(lst: Path) -> str:
    """檢查 HxNames.lst 格式（16 或 64 hex + ':' + 名字）。"""
    try:
        n = 0
        with open(lst, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                s = line.strip()
                if not s or s.startswith('#'):
                    continue
                if ':' not in s:
                    return '第 %d 行沒有冒號' % (n + 1)
                h = s.split(':', 1)[0]
                if len(h) not in (16, 64) or any(c not in '0123456789abcdefABCDEF' for c in h):
                    return '第 %d 行的雜湊格式不對' % (n + 1)
                n += 1
        return '' if n else '檔案是空的'
    except Exception as ex:
        return '%s: %s' % (type(ex).__name__, ex)


# ----------------------------------------------------------------- step 1
def step_key(exe: Path, work: Path, out: Path, game: Path | None = None) -> Path:
    """EXE -> drip_program.json（先看 userdata，再自己算）"""
    # 1) 使用者提供的
    if user_key().exists():
        game_dir = game or exe.parent
        why = validate_key(user_key(), game_dir)
        if not why:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(user_key(), out)
            log('[1/3] 使用 userdata\\drip_program.json（驗證通過）')
            return out
        log('[1/3] userdata\\drip_program.json 不能用（%s），改為自行還原' % why)

    # 2) 自己算
    if not exe.is_file():
        raise SystemExit('找不到 exe: %s' % exe)
    ensure_vendor ()
    if not ensure_dotnet():
        raise SystemExit('找不到 dotnet（需要 .NET SDK 8，x86 版），請先安裝')
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(__import__('vendor').recover_script()),
        '--exe', str(exe),
        '--work-dir', str(work),
        '--out', str(out),
    ]
    log('[1/3] 從 EXE 還原金鑰 ...')
    log('      ' + ' '.join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0 or not out.exists():
        raise SystemExit('金鑰還原失敗（exit %d）' % rc)
    log('      -> %s (%d bytes)' % (out, out.stat().st_size))
    return out


# ----------------------------------------------------------------- step 2
def _game_id(game: Path, exe: Path | None = None) -> str:
    """快取目錄用的 game id：優先用 exe 檔名，其次資料夾名；只留安全字元。"""
    base = (exe.stem if exe is not None else game.name) or 'game'
    safe = re.sub(r'[^0-9A-Za-z_-]+', '_', base).strip('_').lower()
    return (safe or 'game')[:48]


def _exe_path(game: Path, exe: str | None) -> Path | None:
    """CLI 的 --exe：允許絕對路徑，或相對於遊戲資料夾的檔名。"""
    if not exe:
        return None
    p = Path(exe)
    return p if p.is_absolute() else (game / p)


def step_names(game: Path, drip: Path, out: Path, *,
               work: Path | None = None,
               name: str | None = None, exe: Path | None = None) -> Path:
    """遊戲資料 -> HxNames.lst（logic pipeline：解密 corpus → 格式解析 → 邏輯推導 → hash 驗證）"""
    # 1) 使用者提供的清單：直接採用（HXV4_MERGE=1 時仍會再推導）
    if user_names().exists() and os.environ.get('HXV4_MERGE') != '1':
        why = validate_names(user_names())
        if not why:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(user_names(), out)
            log('[2/3] 使用 userdata\\HxNames.lst（驗證通過，%d 行）'
                % len(out.read_text(encoding='utf-8', errors='replace').splitlines()))
            return out
        log('[2/3] userdata\\HxNames.lst 不能用（%s），改為自行推導' % why)

    if not game.is_dir():
        raise SystemExit('找不到遊戲資料夾: %s' % game)
    if not drip.is_file():
        raise SystemExit('找不到 drip_program.json: %s' % drip)
    workdir = Path(work) if work else (ROOT / 'work')
    game_id = name or _game_id(game, exe)
    log('[2/3] 從遊戲資料/腳本邏輯推導檔名（game=%s）...' % game_id)
    sys.path.insert(0, str(LIB))
    ensure_vendor()
    from hxlib.pipeline import Pipeline

    pipeline = Pipeline(game_id, game, drip, work=workdir, out=out, verbose=True)
    summary = pipeline.run()
    log('      -> %s  %d / %d = %.2f%%'
        % (out, summary['recovered'], summary['total_hashes'],
           summary['coverage_pct']))
    return out


def step_corpus(game: Path, drip: Path, work: Path, name: str | None = None,
                exe: Path | None = None) -> Path:
    """只建 corpus 快取（解密所有 entry 並分類；給 --reuse 加速推導）。"""
    if not game.is_dir():
        raise SystemExit('找不到遊戲資料夾: %s' % game)
    if not drip.is_file():
        raise SystemExit('找不到 drip_program.json: %s' % drip)
    workdir = Path(work)
    game_id = name or _game_id(game, exe)
    log('[corpus] 掃描並解密 %s（game=%s）...' % (game, game_id))
    sys.path.insert(0, str(LIB))
    ensure_vendor()
    from hxlib.corpus import Corpus
    c = Corpus(game_id, game, drip, workdir / 'corpus')
    c.scan()
    manifest = c.extract(reuse=True)
    by_kind = {}
    for ce in manifest.values():
        by_kind[ce.kind] = by_kind.get(ce.kind, 0) + 1
    log('[corpus] %d entries: %s' % (len(manifest), by_kind))
    log('[corpus] -> %s' % (workdir / 'corpus' / game_id))
    c.close()
    return workdir / 'corpus' / game_id


# ------------------------------------------------------------------ dynamic
KRKR_DIR = ROOT / 'tools' / 'krkr'


def step_dump(game: Path, exe: Path, seconds: int, work: Path,
               base_names: Path | None, keep_running=False) -> Path | None:
    """讓遊戲自己跑一段，KrkrDump 會把引擎解出來的資產與明文檔名倒出來。

    動態的價值只有一個：補上靜態推導挖不到的檔名（引擎算雜湊前手上有真名）。
    資產本身靜態就能 100% 解出來，所以這裡的產物主要是 `HxNames.dynamic.lst`。
    """
    dll = KRKR_DIR / 'KrkrDump.dll'
    loader = KRKR_DIR / 'KrkrDumpLoader.exe'
    if not dll.is_file() or not loader.is_file():
        log('[dump] 找不到 tools\\krkr\\KrkrDump.dll / KrkrDumpLoader.exe')
        log('       自行編譯：https://github.com/nanami5270/KrkrDump')
        log('       msbuild KrkrDump.sln /p:Configuration=Release /p:Platform=x86 '
            '/p:PlatformToolset=v145')
        return None
    if not str (KRKR_DIR).isascii ():
        log('[dump] 工具目錄含非 ASCII 字元，Detours 注入會失敗：%s' % KRKR_DIR)
        return None

    dump_dir = work / 'dump'
    out_dir = work / 'assets'
    dump_dir.mkdir (parents=True, exist_ok=True)

    cfg = {
        'loglevel': 2,
        'enableExtract': True,
        'outputDirectory': str (dump_dir),
        'rules': [
            r'file://\\./.+?\.xp3>(.+?\..+$)',
            'archive://./(.+)',
            'arc://./(.+)',
            'bres://./(.+)',
        ],
        'includeExtensions': [],
        'excludeExtensions': [],
        'decryptSimpleCrypt': True,
    }
    (KRKR_DIR / 'KrkrDump.json').write_text (
        json.dumps (cfg, ensure_ascii=False, indent=4), encoding='utf-8')
    for p in KRKR_DIR.glob ('KrkrDump-*.log'):
        try:
            p.unlink ()
        except OSError:
            pass

    log('[dump] 啟動遊戲 %d 秒 ...' % seconds)
    proc = subprocess.Popen ([str (loader), str (exe)], cwd=str (KRKR_DIR))
    try:
        for i in range (seconds):
            time.sleep (1)
            if i % 20 == 19:
                log ('       %d/%d 秒' % (i + 1, seconds))
    except KeyboardInterrupt:
        log ('       使用者中斷')
    subprocess.call (['taskkill', '/PID', str (proc.pid), '/T', '/F'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep (1)

    from hxlib import dynlog
    logfile = dynlog.latest_log ([KRKR_DIR])
    if not logfile:
        log ('[dump] 沒有 log —— 注入失敗，或遊戲沒開到任何檔案')
        return None
    log ('[dump] log: %s (%d bytes)' % (logfile, logfile.stat ().st_size))

    rows = dynlog.parse_log (logfile)
    done, missing = dynlog.organize (dump_dir, out_dir, rows)
    log ('[dump] 依 .xp3 歸檔：%d 筆（缺 %d）-> %s' % (done, missing, out_dir))

    harvested = dynlog.harvest (rows)
    names_out = work / 'HxNames.dynamic.lst'
    added, total = dynlog.merge_into (harvested, base_names, names_out)
    log ('[dump] 檔名：抓到 %d 個，新增 %d，總計 %d -> %s'
         % (len (harvested), added, total, names_out))
    return names_out


# ----------------------------------------------------------------- step 3
def step_install(garbro: Path, drip: Path, names: Path, dll: Path | None):
    """把插件放進 GARbro 根目錄、清單與 drip 放進 GameData\\"""
    if not (garbro / 'GameRes.dll').is_file():
        raise SystemExit('不像 GARbro 目錄（找不到 GameRes.dll）: %s' % garbro)
    log('[3/3] 安裝到 GARbro ...')
    target_dll = dll or (ROOT / 'out' / 'ArcHxv4.dll')
    if not target_dll.is_file():
        log('      找不到編譯好的 DLL，改跑 build.ps1 ...')
        rc = subprocess.call(['powershell', '-ExecutionPolicy', 'Bypass', '-File',
                              str(ROOT / 'plugin' / 'build.ps1'),
                              '-GarbroDir', str(garbro),
                              '-OutDir', str(ROOT / 'out')])
        if rc != 0:
            raise SystemExit('DLL 編譯失敗')
        target_dll = ROOT / 'out' / 'ArcHxv4.dll'
    shutil.copy2(target_dll, garbro / 'ArcHxv4.dll')
    data = garbro / 'GameData'
    data.mkdir(parents=True, exist_ok=True)
    shutil.copy2(drip, data / 'drip_program.json')
    shutil.copy2(names, data / 'HxNames.lst')
    log('      -> %s' % (garbro / 'ArcHxv4.dll'))
    log('      -> %s' % (data / 'drip_program.json'))
    log('      -> %s' % (data / 'HxNames.lst'))
    log('完成。開 GARbro.GUI.exe 載入 *.xp3 即可。')


# --------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--work', default=str(ROOT / 'work'), help='中間產物目錄')
    ap.add_argument('--work', default=str(ROOT / 'work'), help='中間產物目錄')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p1 = sub.add_parser('key', help='EXE -> drip_program.json', parents=[common])
    p1.add_argument('--exe', required=True)
    p1.add_argument('--game', default=None, help='遊戲資料夾（驗證金鑰用，預設 = exe 所在目錄）')
    p1.add_argument('--out', default=None)

    p2 = sub.add_parser('names', help='XP3 -> HxNames.lst（logic pipeline）', parents=[common])
    p2.add_argument('--game', required=True)
    p2.add_argument('--exe', default=None,
                    help='遊戲執行檔；沒有 --drip 時會先用它產生 drip_program.json')
    p2.add_argument('--drip', default=None,
                    help='既有 drip_program.json（省略時由 --exe 自動產生）')
    p2.add_argument('--out', default=None)
    p2.add_argument('--name', default=None,
                    help='game id（corpus/report 快取目錄名；預設取 exe 檔名或資料夾名）')

    pc2 = sub.add_parser('corpus', help='只建解密 corpus 快取（給 names 重用）',
                         parents=[common])
    pc2.add_argument('--game', required=True)
    pc2.add_argument('--exe', default=None,
                     help='遊戲執行檔；沒有 --drip 時會先用它產生 drip_program.json')
    pc2.add_argument('--drip', default=None)
    pc2.add_argument('--name', default=None)

    p3 = sub.add_parser('install', help='複製到 GARbro', parents=[common])
    p3.add_argument('--garbro', required=True)
    p3.add_argument('--drip', default=None)
    p3.add_argument('--names', default=None)
    p3.add_argument('--dll', default=None)

    pa = sub.add_parser('all', help='三個階段一次跑完', parents=[common])
    pa.add_argument('--exe', required=True)
    pa.add_argument('--game', required=True)
    pa.add_argument('--garbro', required=True)
    pa.add_argument('--dll', default=None)
    pa.add_argument('--name', default=None, help='game id（快取目錄名）')
    pa.add_argument('--with-dump', type=int, default=0, metavar='SECONDS',
                    help='跑完 names 之後，讓遊戲自己跑 N 秒補抓檔名（需要 tools\\krkr）')

    pd = sub.add_parser('dump', help='讓遊戲跑一段，補抓靜態挖不到的檔名', parents=[common])
    pd.add_argument('--game', required=True)
    pd.add_argument('--exe', required=True)
    pd.add_argument('--seconds', type=int, default=120)
    pd.add_argument('--base', default=None, help='既有的 HxNames.lst（預設 work\\HxNames.lst）')

    pc = sub.add_parser('check', help='只檢查 userdata\\ 裡的檔案能不能用', parents=[common])
    pc.add_argument('--game', required=True)

    args = ap.parse_args(argv)
    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    drip = work / 'drip_program.json'
    names = work / 'HxNames.lst'

    if args.cmd == 'check':
        game = Path(args.game)
        ok = True
        if user_key().exists():
            why = validate_key(user_key(), game)
            log('drip_program.json  : %s' % (('OK' if not why else 'NG  ' + why)))
            ok = ok and not why
        else:
            log('drip_program.json  : (沒有，會自行還原)')
        if user_names().exists():
            why = validate_names(user_names())
            log('HxNames.lst        : %s' % (('OK' if not why else 'NG  ' + why)))
            ok = ok and not why
        else:
            log('HxNames.lst        : (沒有，會自行推導)')
        return 0 if ok else 2

    if args.cmd == 'key':
        step_key(Path(args.exe), work, Path(args.out) if args.out else drip,
                 Path(args.game) if args.game else None)
    elif args.cmd == 'names':
        game = Path(args.game)
        exe = _exe_path(game, args.exe)
        drip_path = Path(args.drip) if args.drip else None
        if drip_path is None:
            if exe is None:
                raise SystemExit('請提供 --exe（自動產生 drip_program.json）或 --drip')
            drip_path = step_key(exe, work, drip, game)
        step_names(game, drip_path,
                   Path(args.out) if args.out else names,
                   work=work, name=args.name, exe=exe)
    elif args.cmd == 'corpus':
        game = Path(args.game)
        exe = _exe_path(game, args.exe)
        drip_path = Path(args.drip) if args.drip else None
        if drip_path is None:
            if exe is None:
                raise SystemExit('請提供 --exe（自動產生 drip_program.json）或 --drip')
            drip_path = step_key(exe, work, drip, game)
        step_corpus(game, drip_path, work, name=args.name, exe=exe)
    elif args.cmd == 'install':
        step_install(Path(args.garbro),
                     Path(args.drip) if args.drip else drip,
                     Path(args.names) if args.names else names,
                     Path(args.dll) if args.dll else None)
    elif args.cmd == 'dump':
        game = Path(args.game)
        exe = Path(args.exe)
        if not exe.is_absolute():
            exe = game / exe
        base = Path(args.base) if args.base else (names if names.is_file() else None)
        step_dump(game, exe, args.seconds, work, base)
    else:
        game = Path(args.game)
        exe = _exe_path(game, args.exe)
        step_key(exe, work, drip, game)
        step_names(game, drip, names,
                   work=work, name=args.name, exe=exe)
        if args.with_dump:
            dyn = step_dump(game, exe, args.with_dump, work,
                            names if names.is_file() else None)
            if dyn:
                names = dyn
        step_install(Path(args.garbro), drip, names,
                     Path(args.dll) if args.dll else None)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())









