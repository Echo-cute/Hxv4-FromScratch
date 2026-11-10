"""vendor.py -- 執行期取得 cxdec-hxv4-static-analysis（本專案不隨附該專案的程式碼）。

該上游專案沒有附授權條款，為了避免再散布爭議，本專案改為第一次執行時
自動下載到 `vendor/`，之後重複使用。

**正常情況直接從公開上游抓**；只有在公開來源失敗時才退回鏡像
（預設鏡像 `Echo-cute/cxdec-vendor`，可用環境變數改掉）。

環境變數：
    HXV4_VENDOR_URL   指定要優先使用的 git 來源
                      （例：https://github.com/you/cxdec-vendor.git）
    HXV4_VENDOR_ZIP   指定要優先使用的 zip 來源
    HXV4_VENDOR_MIRROR  備援 git 來源（預設 Echo-cute/cxdec-vendor）

公開 API：
    setup()            把上游的 src 加進 sys.path（可重複呼叫）
    src()              回傳 `<vendor>/.../src`，必要時先下載
    recover_script()   回傳 `static_xp3_recover.py` 的路徑
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 專案根目錄
VENDOR = ROOT / 'vendor'

UPSTREAM = 'hktkqj/cxdec-hxv4-static-analysis'
BRANCH = 'main'
MIRROR = 'Echo-cute/cxdec-vendor'

GIT_PRIMARY = 'https://github.com/%s.git' % UPSTREAM
ZIP_PRIMARY = 'https://codeload.github.com/%s/zip/refs/heads/%s' % (UPSTREAM, BRANCH)
GIT_MIRROR = 'https://github.com/%s.git' % MIRROR

_ready = False


def _sources ():
    """回傳 [(kind, url)]，依嘗試順序。公開上游優先，鏡像只當備援。"""
    out = []
    env_git = os.environ.get ('HXV4_VENDOR_URL')
    env_zip = os.environ.get ('HXV4_VENDOR_ZIP')
    if env_git:
        out.append (('git', env_git))
    if env_zip:
        out.append (('zip', env_zip))
    out.append (('git', GIT_PRIMARY))
    out.append (('zip', ZIP_PRIMARY))
    # 預設只從「原始上游」下載；鏡像僅在使用者明確指定環境變數時才用
    mirror_git = os.environ.get ('HXV4_VENDOR_MIRROR')
    if mirror_git and mirror_git != env_git:
        out.append (('git', mirror_git))
    return out


def _find_src () -> Path | None:
    for cand in sorted (VENDOR.glob ('*/src')):
        if (cand / 'common' / 'xp3_inspect.py').is_file ():
            return cand
    return None


def _try_git (url: str) -> Path | None:
    if not shutil.which ('git'):
        return None
    dst = VENDOR / (url.rstrip ('/').rsplit ('/', 1)[-1].replace ('.git', ''))
    shutil.rmtree (dst, ignore_errors=True)
    print ('[vendor] git clone %s ...' % url)
    if subprocess.call (['git', 'clone', '--depth', '1', url, str (dst)]) != 0:
        shutil.rmtree (dst, ignore_errors=True)
        return None
    src = dst / 'src'
    return src if (src / 'common' / 'xp3_inspect.py').is_file () else None


def _try_zip (url: str) -> Path | None:
    zpath = VENDOR / 'cxdec.zip'
    print ('[vendor] 下載 %s ...' % url)
    try:
        req = urllib.request.Request (url, headers={'User-Agent': 'hxv4-from-scratch'})
        with urllib.request.urlopen (req, timeout=120) as resp, open (zpath, 'wb') as fh:
            shutil.copyfileobj (resp, fh)
        with zipfile.ZipFile (zpath) as zf:
            zf.extractall (VENDOR)
    except Exception as ex:
        print ('[vendor] 失敗：%s' % ex)
        return None
    finally:
        zpath.unlink (missing_ok=True)
    return _find_src ()


def _download () -> Path:
    VENDOR.mkdir (parents=True, exist_ok=True)
    for kind, url in _sources ():
        src = _try_git (url) if kind == 'git' else _try_zip (url)
        if src:
            return src
        print ('[vendor] %s 失敗，換下一個來源' % url)
    raise RuntimeError (
        '無法取得 cxdec-hxv4-static-analysis。請確認網路，或手動把上游 clone 到 '
        '%s\\，或用 HXV4_VENDOR_URL 指定鏡像。' % VENDOR)


def src () -> Path:
    found = _find_src ()
    return found if found else _download ()


def setup () -> Path:
    """把上游的 src（含 src/common）加進 sys.path。"""
    global _ready
    s = src ()
    for p in (str (s), str (s / 'common')):
        if p not in sys.path:
            sys.path.insert (0, p)
    _ready = True
    return s


def recover_script () -> Path:
    return src () / 'static_extract' / 'static_xp3_recover.py'

# ---------------------------------------------------------------------------
# 自動安裝 / 下載第三方元件（執行時 bootstrap）
# ---------------------------------------------------------------------------
# KrkrDump 原始來源（crskycode/KrkrDump，GPL-3.0）的官方 release
KRKR_RELEASE_URL = os.environ.get(
    'HXV4_KRKR_URL',
    'https://github.com/crskycode/KrkrDump/releases/download/v1.5/v1.5.zip')
KRKR_FILES = ('KrkrDump.dll', 'KrkrDumpLoader.exe')
KRKR_JSON = 'KrkrDump.json'


def _ts(*a) -> None:
    import time
    print('[%s]' % time.strftime('%H:%M:%S'), *a, flush=True)


def _http_download(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': 'hxv4-from-scratch'})
        with urllib.request.urlopen(req, timeout=120) as resp, \
                open(dest, 'wb') as fh:
            shutil.copyfileobj(resp, fh)
        return dest.is_file() and dest.stat().st_size > 0
    except Exception as ex:
        _ts('[bootstrap] 下載失敗 %s: %s' % (url, ex))
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def ensure_python_deps(verbose: bool = True) -> bool:
    """確保 PyCryptodome 可用；缺少時自動 pip install requirements.txt。"""
    import importlib.util
    if importlib.util.find_spec('Crypto') is not None:
        if verbose:
            _ts('[bootstrap] Python 套件 OK（PyCryptodome）')
        return True
    if os.environ.get('HXV4_SKIP_AUTO_INSTALL'):
        _ts('[bootstrap] 缺少 PyCryptodome；請手動 pip install -r requirements.txt')
        return False
    req = ROOT / 'requirements.txt'
    cmd = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check']
    cmd += ['-r', str(req)] if req.is_file() else ['pycryptodome>=3.15']
    _ts('[bootstrap] 缺少 PyCryptodome，自動安裝：%s' % ' '.join(cmd))
    try:
        rc = subprocess.call(cmd)
    except Exception as ex:
        _ts('[bootstrap] pip 執行失敗：%s' % ex)
        return False
    if rc != 0:
        _ts('[bootstrap] pip install 失敗（exit=%d）' % rc)
        return False
    importlib.invalidate_caches()
    ok = importlib.util.find_spec('Crypto') is not None
    if verbose:
        _ts('[bootstrap] PyCryptodome 安裝%s' % ('完成' if ok else '失敗'))
    return ok


def _krkr_source_dirs() -> list[Path]:
    """可能已經有 KrkrDump 備份的本機目錄（依序試）。"""
    dirs = []
    env = os.environ.get('HXV4_KRKR_SOURCE')
    if env:
        dirs.append(Path(env))
    dirs += [ROOT.parent / 'cxdec-vendor' / 'third_party' / 'krkr',
             ROOT / 'vendor' / 'cxdec-vendor' / 'third_party' / 'krkr',
             ROOT / 'third_party' / 'krkr']
    return dirs


def _install_krkr_zip(zip_path: Path, krkr: Path, verbose: bool = True) -> bool:
    """解開原始 release zip，找出 KrkrDump.dll / KrkrDumpLoader.exe 並複製。"""
    import tempfile
    ok = True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(tmp)
            found = {}
            for name in KRKR_FILES:
                hits = sorted(Path(tmp).rglob(name))
                if hits:
                    found[name] = hits[0]
            for name in KRKR_FILES:
                if name in found:
                    shutil.copy2(found[name], krkr / name)
                    if verbose:
                        _ts('[bootstrap] 安裝 %s（原始 release）' % name)
                else:
                    ok = False
    except Exception as ex:
        _ts('[bootstrap] 解壓 KrkrDump release 失敗：%s' % ex)
        return False
    return ok


def ensure_krkrdump(verbose: bool = True) -> bool:
    """確保 tools/krkr 有 KrkrDump 二進位。

    下載來源是**原始上游**的官方 release（crskycode/KrkrDump），不是鏡像。
    只有在原始來源抓不到時，才退回本機既有的 cxdec-vendor 備份（並明確標示）。
    """
    krkr = ROOT / 'tools' / 'krkr'
    krkr.mkdir(parents=True, exist_ok=True)
    missing = [f for f in KRKR_FILES if not (krkr / f).is_file()]
    if not missing:
        if verbose:
            _ts('[bootstrap] KrkrDump 二進位 OK（%s）' % krkr)
        return True
    if os.environ.get('HXV4_SKIP_AUTO_INSTALL'):
        _ts('[bootstrap] 缺少 KrkrDump：%s' % ', '.join(missing))
        return False

    # 1) 原始上游 official release
    ok = False
    try:
        _ts('[bootstrap] 從原始來源下載 KrkrDump release：%s' % KRKR_RELEASE_URL)
        zpath = krkr / '_krkrdump_release.zip'
        if _http_download(KRKR_RELEASE_URL, zpath):
            ok = _install_krkr_zip(zpath, krkr, verbose)
        try:
            zpath.unlink(missing_ok=True)
        except OSError:
            pass
    except Exception as ex:
        _ts('[bootstrap] 原始來源下載失敗：%s' % ex)
    if ok and all((krkr / f).is_file() for f in KRKR_FILES):
        if verbose:
            _ts('[bootstrap] KrkrDump 準備完成（原始來源 crskycode/KrkrDump）')
        return True

    # 2) 原始來源失敗 → 本機備份（不是鏡像下載，僅離線備援）
    for name in KRKR_FILES:
        if (krkr / name).is_file():
            continue
        for source in _krkr_source_dirs():
            cand = source / name
            if cand.is_file():
                shutil.copy2(cand, krkr / name)
                if verbose:
                    _ts('[bootstrap] 原始來源失敗，改用本機備份複製 %s <- %s'
                        % (name, cand))
                break
    ok = all((krkr / f).is_file() for f in KRKR_FILES)
    if verbose:
        _ts('[bootstrap] KrkrDump 準備%s' % ('完成' if ok else '失敗（動態 dump 停用）'))
    return ok


def _find_vcvars() -> tuple[Path | None, Path | None]:
    """回傳 (vcvarsall.bat, cl.exe) 路徑；找不到回 (None, None)。"""
    roots = [
        Path(r'C:\Program Files (x86)\Microsoft Visual Studio'),
        Path(r'C:\Program Files\Microsoft Visual Studio'),
    ]
    for root in roots:
        for vcvars in root.glob('*/BuildTools/VC/Auxiliary/Build/vcvarsall.bat'):
            cl = list(vcvars.parents[3].glob(
                'VC/Tools/MSVC/*/bin/Hostx64/x86/cl.exe'))
            return vcvars, (cl[0] if cl else None)
    return None, None


def ensure_inject_launcher(verbose: bool = True) -> bool:
    """tools/krkr/inject_launch.exe 缺少時嘗試用本機 MSVC 自動編譯。"""
    krkr = ROOT / 'tools' / 'krkr'
    exe = krkr / 'inject_launch.exe'
    if exe.is_file():
        if verbose:
            _ts('[bootstrap] inject_launch.exe OK')
        return True
    src = krkr / 'inject_launch.c'
    if not src.is_file() or os.environ.get('HXV4_SKIP_AUTO_INSTALL'):
        return False
    vcvars, cl = _find_vcvars()
    if not vcvars:
        _ts('[bootstrap] 找不到 MSVC，略過 inject_launch 編譯（動態 dump 可退回 loader）')
        return False
    _ts('[bootstrap] 自動編譯 inject_launch.exe ...')
    bat = krkr / '_build_inject.bat'
    try:
        bat.write_text(
            '@echo off\r\n'
            'call "%s" x86 >nul 2>&1\r\n'
            'if errorlevel 1 exit /b 2\r\n'
            'cl /nologo /MT /O2 "%s" /Fe:"%s"\r\n'
            % (vcvars, src, exe), encoding='ascii')
        proc = subprocess.run(['cmd', '/c', str(bat)], cwd=str(krkr),
                              capture_output=True, text=True,
                              errors='replace', timeout=180)
    except Exception as ex:
        _ts('[bootstrap] 編譯失敗：%s' % ex)
        return False
    finally:
        try:
            bat.unlink()
        except OSError:
            pass
    ok = exe.is_file()
    if not ok:
        tail = ((proc.stdout or '') + (proc.stderr or '')).strip()
        _ts('[bootstrap] 編譯失敗（exit=%d）%s' %
            (proc.returncode, tail.splitlines()[-1] if tail else ''))
    else:
        _ts('[bootstrap] inject_launch 編譯完成')
    return ok


_BOOTSTRAP_DONE: dict = {}


def bootstrap(verbose: bool = True, need_pycryptodome: bool = True,
              need_cxdec: bool = True, need_krkr: bool = False) -> dict:
    """執行前自動準備第三方元件；回傳各項目狀態。

    * 只在主程序執行；ProcessPool 的 worker（spawn）會重新 import 主模組，
      這裡直接靜默返回，避免 log 被大量重複的 bootstrap 訊息洗版。
    * 同一程序重複呼叫只會做一次。
    """
    import multiprocessing as _mp
    if _mp.parent_process() is not None:
        return {}
    key = (need_pycryptodome, need_cxdec, need_krkr)
    if key in _BOOTSTRAP_DONE:
        return _BOOTSTRAP_DONE[key]
    status = {}
    _BOOTSTRAP_DONE[key] = status
    if need_pycryptodome:
        status['pycryptodome'] = ensure_python_deps(verbose)
    if need_cxdec:
        before = _find_src()
        if before and verbose:
            _ts('[bootstrap] cxdec vendor OK（%s）' % before.parent)
        try:
            setup()
            status['cxdec'] = True
            if not before and verbose:
                _ts('[bootstrap] cxdec vendor 下載完成')
        except Exception as ex:
            _ts('[bootstrap] cxdec vendor 不可用：%s' % ex)
            status['cxdec'] = False
    if need_krkr:
        status['krkrdump'] = ensure_krkrdump(verbose)
        status['inject_launch'] = ensure_inject_launcher(verbose)
    return status
