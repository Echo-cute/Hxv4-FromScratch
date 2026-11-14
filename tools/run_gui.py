#!/usr/bin/env python3
"""run_gui.py -- Hxv4 檔名推導工具（Tkinter 圖形介面，顯示即時 log 與進度）。

run_gui.py -- Hxv4 filename recovery tool (Tkinter GUI with live log/progress).

用法 / Usage::

    python tools/run_gui.py [--lang zh|en]

GUI 會以背景程序執行 ``tools/run_pipeline.py``，即時顯示：

* 目前階段（N/13）與進度條（%/耗時/速度）
* 完整 log（同時寫入 work/logs/<game>_<timestamp>.log）
* 完成後自動安裝到指定的 GARbro 目錄

填好遊戲資料夾與遊戲執行檔即可；程式會在推導前自動產生
``drip_program.json``（不需要事先準備）。

視窗語言可在「語言 / Language」下拉選單切換（預設依系統語系：中文語系用
中文，其餘用英文）。

無需任何第三方套件；Tkinter 是 Python 內建。

The window language can be switched with the "語言 / Language" drop-down
(default: Traditional Chinese for Chinese locales, English otherwise).
"""
from __future__ import annotations

import locale
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

ROOT = Path(__file__).resolve().parent.parent
WORK_DIR = ROOT / 'work'
DEFAULT_OUT_NAME = 'HxNames.lst'
STAGE_RE = re.compile(r'=====\s*\[(\d+)/(\d+)\]\s*(.*?)\s*=====')
RATIO_RE = re.compile(r'(\d[\d,]*)\s*/\s*(\d[\d,]*)')
PCT_RE = re.compile(r'(\d+(?:\.\d+)?)%')
# 遊戲 ID 會成為輸出檔名的一部分，因此排除 Windows 檔名不允許的字元。
BAD_ID_CHARS = frozenset('\\/:*?"<>|')

LANGUAGES = (('zh', '中文（繁體）'), ('en', 'English'))
LANG_CODES = {name: code for code, name in LANGUAGES}
LANG_NAMES = {code: name for code, name in LANGUAGES}

TEXTS = {
    'zh': {
        'title': 'Hxv4 檔名推導工具',
        'language': '語言',
        'game_id': '遊戲 ID',
        'game_dir': '遊戲目錄',
        'game_exe': '遊戲執行檔 (.exe)',
        'work_dir': '工作目錄',
        'out_list': '輸出清單',
        'garbro_root': 'GARbro 根目錄（GARbro.GUI.exe 所在）',
        'browse': '瀏覽…',
        'start': '▶ 開始推導',
        'stop': '⏹ 停止',
        'open_logs': '📂 開啟 log 資料夾',
        'idle': '待機中',
        'starting': '啟動中…',
        'done': '完成 ✓',
        'failed': '失敗（exit=%d）',
        'missing_args': ('缺少參數', '請填遊戲 ID、遊戲目錄與遊戲執行檔 (.exe)'),
        'exe_not_found': ('找不到執行檔', '%s'),
        'bad_game_id': ('遊戲 ID 無效', '遊戲 ID 不能含有下列字元：\\ / : * ? " < > |'),
        'start_failed': ('啟動失敗', '%s'),
        'running': ('程式仍在執行', '推導仍在執行，要停止並關閉嗎？'),
        'stopped': '--- 使用者停止，終止程序 ---',
        'exec': '=== 執行: %s ===',
        'exit': '=== 結束（exit=%d）===',
    },
    'en': {
        'title': 'Hxv4 Filename Recovery Tool',
        'language': 'Language',
        'game_id': 'Game ID',
        'game_dir': 'Game folder',
        'game_exe': 'Game executable (.exe)',
        'work_dir': 'Working folder',
        'out_list': 'Output list',
        'garbro_root': 'GARbro root folder (where GARbro.GUI.exe is)',
        'browse': 'Browse…',
        'start': '▶ Start',
        'stop': '⏹ Stop',
        'open_logs': '📂 Open log folder',
        'idle': 'Idle',
        'starting': 'Starting…',
        'done': 'Done ✓',
        'failed': 'Failed (exit=%d)',
        'missing_args': ('Missing arguments',
                         'Please fill in the game ID, game folder and '
                         'game executable (.exe).'),
        'exe_not_found': ('Executable not found', '%s'),
        'bad_game_id': ('Invalid game ID',
                        'The game ID cannot contain any of these characters: '
                        '\\ / : * ? " < > |'),
        'start_failed': ('Failed to start', '%s'),
        'running': ('Still running',
                    'Recovery is still running. Stop it and close the window?'),
        'stopped': '--- Stopped by user, terminating process ---',
        'exec': '=== Running: %s ===',
        'exit': '=== Finished (exit=%d) ===',
    },
}


def detect_language() -> str:
    """依系統語系選擇介面語言（中文語系 -> zh，其餘 -> en）。"""
    if sys.platform == 'win32':
        # Windows：GetUserDefaultUILanguage 的 primary language ID 0x04 = 中文
        try:
            import ctypes
            lcid = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            return 'zh' if (lcid & 0xFF) == 0x04 else 'en'
        except Exception:
            pass
    loc = ''
    for var in ('LANGUAGE', 'LC_ALL', 'LC_MESSAGES', 'LANG'):
        val = os.environ.get(var)
        if val:
            loc = val
            break
    if not loc:
        try:
            loc = locale.getlocale()[0] or locale.getdefaultlocale()[0] or ''
        except Exception:
            loc = ''
    loc = loc.lower().replace('-', '_')
    if loc.startswith('zh') or loc.startswith('chinese'):
        return 'zh'
    return 'en'


class App:
    def __init__(self, root: tk.Tk, lang: str | None = None) -> None:
        self.root = root
        self.lang = lang if lang in TEXTS else detect_language()
        self.proc: subprocess.Popen | None = None
        self.reader: threading.Thread | None = None
        self.q: queue.Queue[str] = queue.Queue()
        self.log_file: Path | None = None
        self.stage_total = 13
        self._labels: list[tuple[ttk.Label, str]] = []
        self._buttons: list[tuple[ttk.Button, str]] = []
        self._status: tuple[str, object] = ('idle', None)
        self._stage_text: str | None = None
        self._syncing_out = False
        self._out_custom = False
        self._build_ui()

    # ---------------------------------------------------------------- i18n
    def t(self, key: str) -> str:
        return TEXTS[self.lang][key]  # type: ignore[return-value]

    def _retranslate(self) -> None:
        self.root.title(self.t('title'))
        for widget, key in self._labels:
            widget.configure(text=self.t(key))
        for widget, key in self._buttons:
            widget.configure(text=self.t(key))
        self._render_status()

    def _on_lang_selected(self, _event=None) -> None:
        code = LANG_CODES.get(self.v_lang.get())
        if code and code != self.lang:
            self.lang = code
            self._retranslate()

    def _set_status(self, key: str, arg=None) -> None:
        self._status = (key, arg)
        self._stage_text = None
        self._render_status()

    def _render_status(self) -> None:
        if self._stage_text is not None:
            text = self._stage_text
        else:
            key, arg = self._status
            text = self.t(key)
            if arg is not None:
                text = text % arg
        self.lbl_stage.configure(text=text)

    # ------------------------------------------------------------ out sync
    def _default_out_path(self, game: str = '') -> Path:
        """預設輸出清單路徑：<工作目錄>/HxNames.<遊戲 ID>.lst（無 ID 時 HxNames.lst）。"""
        work = self.v_work.get().strip() or str(WORK_DIR)
        name = ('HxNames.%s.lst' % game) if game else DEFAULT_OUT_NAME
        return Path(work) / name

    def _sync_output(self, *_args) -> None:
        """輸出清單仍是預設路徑時，跟著遊戲 ID／工作目錄自動改名。"""
        if self._out_custom:
            return
        self._syncing_out = True
        try:
            self.v_out.set(str(self._default_out_path(
                self.v_game.get().strip())))
        finally:
            self._syncing_out = False

    def _on_out_changed(self, *_args) -> None:
        if self._syncing_out:
            return
        value = self.v_out.get().strip()
        self._out_custom = bool(
            value and value != str(self._default_out_path(
                self.v_game.get().strip())))

    # ------------------------------------------------------------------ UI
    def _row(self, parent, key, row, default='', browse=None):
        label = ttk.Label(parent, text=self.t(key))
        label.grid(row=row, column=0, sticky='w', padx=(8, 4), pady=3)
        self._labels.append((label, key))
        var = tk.StringVar(value=default)
        ent = ttk.Entry(parent, textvariable=var, width=62)
        ent.grid(row=row, column=1, sticky='we', padx=4, pady=3)
        if browse:
            btn = ttk.Button(parent, text=self.t('browse'), width=0,
                             command=lambda: self._browse(var, browse))
            btn.grid(row=row, column=2, padx=(4, 8), pady=3)
            self._buttons.append((btn, 'browse'))
        return var

    def _browse(self, var, kind):
        initial = var.get().strip()
        if kind == 'dir':
            opts = {}
            if initial and Path(initial).is_dir():
                opts['initialdir'] = initial
            p = filedialog.askdirectory(**opts)
        elif kind == 'save':
            current = Path(initial) if initial else self._default_out_path(
                self.v_game.get().strip())
            opts = {'defaultextension': '.lst',
                    'initialfile': current.name}
            if current.parent.is_dir():
                opts['initialdir'] = str(current.parent)
            p = filedialog.asksaveasfilename(**opts)
        else:
            opts = {}
            if initial and Path(initial).is_file():
                opts['initialdir'] = str(Path(initial).parent)
            p = filedialog.askopenfilename(**opts)
        if p:
            var.set(p)

    def _build_ui(self) -> None:
        self.root.title(self.t('title'))
        self.root.geometry('980x700')
        top = ttk.Frame(self.root)
        top.pack(fill='x', padx=6, pady=6)
        top.columnconfigure(1, weight=1)

        lang_label = ttk.Label(top, text=self.t('language'))
        lang_label.grid(row=0, column=0, sticky='w', padx=(8, 4), pady=3)
        self._labels.append((lang_label, 'language'))
        self.v_lang = tk.StringVar(value=LANG_NAMES[self.lang])
        self.cmb_lang = ttk.Combobox(
            top, textvariable=self.v_lang, width=16, state='readonly',
            values=[name for _code, name in LANGUAGES])
        self.cmb_lang.grid(row=0, column=1, sticky='w', padx=4, pady=3)
        self.cmb_lang.bind('<<ComboboxSelected>>', self._on_lang_selected)

        self.v_game = self._row(top, 'game_id', 1, '')
        self.v_dir = self._row(top, 'game_dir', 2, '', 'dir')
        self.v_exe = self._row(top, 'game_exe', 3, '', 'file')
        self.v_work = self._row(top, 'work_dir', 4,
                                str(WORK_DIR), 'dir')
        self.v_out = self._row(top, 'out_list', 5,
                               str(WORK_DIR / DEFAULT_OUT_NAME), 'save')
        self.v_garbro = self._row(top, 'garbro_root', 6, '', 'dir')

        self.v_game.trace_add('write', self._sync_output)
        self.v_work.trace_add('write', self._sync_output)
        self.v_out.trace_add('write', self._on_out_changed)

        bar = ttk.Frame(self.root)
        bar.pack(fill='x', padx=8, pady=(0, 4))
        self.btn_run = ttk.Button(bar, text=self.t('start'), width=0,
                                  command=self.start)
        self.btn_run.pack(side='left')
        self.btn_stop = ttk.Button(bar, text=self.t('stop'), width=0,
                                   command=self.stop, state='disabled')
        self.btn_stop.pack(side='left', padx=6)
        self.btn_logdir = ttk.Button(bar, text=self.t('open_logs'), width=0,
                                     command=self.open_logdir,
                                     state='disabled')
        self.btn_logdir.pack(side='left', padx=6)
        self._buttons.extend([(self.btn_run, 'start'),
                              (self.btn_stop, 'stop'),
                              (self.btn_logdir, 'open_logs')])

        self.lbl_stage = ttk.Label(self.root, text='', anchor='w')
        self.lbl_stage.pack(fill='x', padx=10, pady=(4, 0))
        self.progress = ttk.Progressbar(self.root, mode='determinate',
                                        maximum=100)
        self.progress.pack(fill='x', padx=10, pady=4)
        self.lbl_detail = ttk.Label(self.root, text='', anchor='w')
        self.lbl_detail.pack(fill='x', padx=10)

        self.log = ScrolledText(self.root, height=28, state='disabled',
                                font=('Consolas', 9), wrap='none',
                                background='#111', foreground='#ddd',
                                insertbackground='#ddd')
        self.log.pack(fill='both', expand=True, padx=8, pady=8)

        self._render_status()
        self.root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.root.after(120, self._poll)

    # ------------------------------------------------------------- actions
    def _append(self, text: str) -> None:
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        # 保留最後 4000 行避免長時間跑爆記憶體
        lines = int(self.log.index('end-1c').split('.')[0])
        if lines > 4000:
            self.log.delete('1.0', '%d.0' % (lines - 4000))
        self.log.see('end')
        self.log.configure(state='disabled')

    def open_logdir(self) -> None:
        if self.log_file:
            os.startfile(str(self.log_file.parent))

    def start(self) -> None:
        if self.proc is not None:
            return
        game = self.v_game.get().strip()
        game_dir = self.v_dir.get().strip()
        exe = self.v_exe.get().strip()
        title, msg = self.t('missing_args')
        if not (game and game_dir and exe):
            messagebox.showerror(title, msg)
            return
        if any(ch in BAD_ID_CHARS for ch in game):
            messagebox.showerror(*self.t('bad_game_id'))
            return
        if not Path(exe).is_file():
            title, msg = self.t('exe_not_found')
            messagebox.showerror(title, msg % exe)
            return
        work = self.v_work.get().strip() or str(WORK_DIR)
        out = self.v_out.get().strip() or str(self._default_out_path(game))
        cmd = [sys.executable, str(ROOT / 'tools' / 'run_pipeline.py'),
               '--game', game, '--game-dir', game_dir, '--exe', exe,
               '--work', work, '--out', out]
        garbro = self.v_garbro.get().strip()
        if garbro:
            cmd += ['--garbro-dir', garbro]
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        self.progress['value'] = 0
        self._set_status('starting')
        self.lbl_detail.configure(text='')
        self.btn_run.configure(state='disabled')
        self.btn_stop.configure(state='normal')
        self._append('\n' + self.t('exec') % ' '.join(cmd))
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                errors='replace', bufsize=1, env=env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception as ex:
            title, msg = self.t('start_failed')
            messagebox.showerror(title, msg % str(ex))
            self._set_status('idle')
            self._finish()
            return
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.q.put(line.rstrip('\n'))

    def stop(self) -> None:
        if not self.proc:
            return
        self._append(self.t('stopped'))
        try:
            subprocess.call(['taskkill', '/T', '/F', '/PID',
                             str(self.proc.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
        except Exception:
            self.proc.terminate()

    def _finish(self) -> None:
        self.proc = None
        self.btn_run.configure(state='normal')
        self.btn_stop.configure(state='disabled')

    def on_close(self) -> None:
        if self.proc:
            if not messagebox.askyesno(*self.t('running')):
                return
            self.stop()
            time.sleep(0.3)
        self.root.destroy()

    # ------------------------------------------------------------- polling
    def _poll(self) -> None:
        try:
            while True:
                line = self.q.get_nowait()
                self._handle_line(line)
        except queue.Empty:
            pass
        if self.proc is not None and self.proc.poll() is not None:
            rc = self.proc.returncode
            self._append('\n' + self.t('exit') % rc)
            self.progress['value'] = 100 if rc == 0 else self.progress['value']
            if rc == 0:
                self._set_status('done')
            else:
                self._set_status('failed', rc)
            self._finish()
            if self.log_file:
                self.btn_logdir.configure(state='normal')
        self.root.after(120, self._poll)

    def _handle_line(self, line: str) -> None:
        self._append(line)
        m = STAGE_RE.search(line)
        if m:
            idx, total, name = int(m.group(1)), int(m.group(2)), m.group(3)
            self.stage_total = max(total, 1)
            self._stage_text = '[%d/%d] %s' % (idx, total, name)
            self._render_status()
            self.progress['value'] = max(0.0, (idx - 1) * 100.0 / total)
            return
        lm = re.search(r'log file:\s*(.+)$', line)
        if lm:
            try:
                self.log_file = Path(lm.group(1).strip())
                self.btn_logdir.configure(state='normal')
            except Exception:
                pass
        ratio = RATIO_RE.search(line)
        pct = PCT_RE.search(line)
        if ratio:
            try:
                done = int(ratio.group(1).replace(',', ''))
                total = int(ratio.group(2).replace(',', ''))
                if total > 0:
                    frac = done / total
                    stage = max(0, self.progress['value'] / 100.0)
                    self.progress['value'] = min(99.0,
                                                 (stage * self.stage_total +
                                                  frac) / self.stage_total * 100)
                self.lbl_detail.configure(text=line.strip()[:160])
            except ValueError:
                pass
        elif pct:
            self.lbl_detail.configure(text=line.strip()[:160])


def _parse_lang(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg == '--lang' and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
        if arg.startswith('--lang='):
            return arg.split('=', 1)[1].strip().lower()
    return None


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    lang = _parse_lang(argv)
    if lang not in TEXTS:
        lang = None
    root = tk.Tk()
    App(root, lang)
    if '--self-test' in argv:
        root.after(800, root.destroy)
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
