#!/usr/bin/env python3
"""run_gui.py -- Hxv4 檔名推導工具（Tkinter 圖形介面，顯示即時 log 與進度）。

用法::

    python tools/run_gui.py

GUI 會以背景程序執行 ``tools/run_pipeline.py``，即時顯示：
* 目前階段（N/13）與進度條（%/耗時/速度）
* 完整 log（同時寫入 work/logs/<game>_<timestamp>.log）
* 完成後自動安裝到指定的 GARbro 目錄

填好遊戲資料夾與遊戲執行檔即可；程式會在推導前自動產生
``drip_program.json``（不需要事先準備）。

無需任何第三方套件；Tkinter 是 Python 內建。
"""
from __future__ import annotations

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
STAGE_RE = re.compile(r'=====\s*\[(\d+)/(\d+)\]\s*(.*?)\s*=====')
RATIO_RE = re.compile(r'(\d[\d,]*)\s*/\s*(\d[\d,]*)')
PCT_RE = re.compile(r'(\d+(?:\.\d+)?)%')


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.reader: threading.Thread | None = None
        self.q: queue.Queue[str] = queue.Queue()
        self.log_file: Path | None = None
        self.stage_total = 13
        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _row(self, parent, label, row, default='', browse=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w',
                                          padx=(8, 4), pady=3)
        var = tk.StringVar(value=default)
        ent = ttk.Entry(parent, textvariable=var, width=62)
        ent.grid(row=row, column=1, sticky='we', padx=4, pady=3)
        if browse:
            ttk.Button(parent, text='瀏覽…', width=7,
                       command=lambda: self._browse(var, browse)).grid(
                row=row, column=2, padx=(4, 8), pady=3)
        return var

    def _browse(self, var, kind):
        if kind == 'dir':
            p = filedialog.askdirectory()
        elif kind == 'save':
            p = filedialog.asksaveasfilename(defaultextension='.lst')
        else:
            p = filedialog.askopenfilename()
        if p:
            var.set(p)

    def _build_ui(self) -> None:
        self.root.title('Hxv4 檔名推導工具')
        self.root.geometry('980x700')
        top = ttk.Frame(self.root)
        top.pack(fill='x', padx=6, pady=6)
        top.columnconfigure(1, weight=1)

        self.v_game = self._row(top, '遊戲 ID', 0, 'lllj')
        self.v_dir = self._row(top, '遊戲目錄', 1, '', 'dir')
        self.v_exe = self._row(top, '遊戲執行檔 (.exe)', 2, '', 'file')
        self.v_work = self._row(top, '工作目錄', 3, str(ROOT / 'work'), 'dir')
        self.v_out = self._row(top, '輸出清單', 4,
                               str(ROOT / 'work' / 'HxNames.lllj.lst'), 'save')
        self.v_garbro = self._row(top, 'GARbro 根目錄（GARbro.GUI.exe 所在）', 5, '', 'dir')

        bar = ttk.Frame(self.root)
        bar.pack(fill='x', padx=8, pady=(0, 4))
        self.btn_run = ttk.Button(bar, text='▶ 開始推導', command=self.start)
        self.btn_run.pack(side='left')
        self.btn_stop = ttk.Button(bar, text='⏹ 停止', command=self.stop,
                                   state='disabled')
        self.btn_stop.pack(side='left', padx=6)
        self.btn_logdir = ttk.Button(bar, text='📂 開啟 log 資料夾',
                                     command=self.open_logdir, state='disabled')
        self.btn_logdir.pack(side='left', padx=6)

        self.lbl_stage = ttk.Label(self.root, text='待機中', anchor='w')
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
        if not (game and game_dir and exe):
            messagebox.showerror('缺少參數', '請填遊戲 ID、遊戲目錄與遊戲執行檔 (.exe)')
            return
        if not Path(exe).is_file():
            messagebox.showerror('找不到執行檔', exe)
            return
        out = self.v_out.get().strip() or str(ROOT / 'work' / ('HxNames.%s.lst' % game))
        cmd = [sys.executable, str(ROOT / 'tools' / 'run_pipeline.py'),
               '--game', game, '--game-dir', game_dir, '--exe', exe,
               '--work', self.v_work.get().strip(), '--out', out]
        garbro = self.v_garbro.get().strip()
        if garbro:
            cmd += ['--garbro-dir', garbro]
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        self.progress['value'] = 0
        self.lbl_stage.configure(text='啟動中…')
        self.lbl_detail.configure(text='')
        self.btn_run.configure(state='disabled')
        self.btn_stop.configure(state='normal')
        self._append('\n=== 執行: %s ===' % ' '.join(cmd))
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                errors='replace', bufsize=1, env=env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except Exception as ex:
            messagebox.showerror('啟動失敗', str(ex))
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
        self._append('--- 使用者停止，終止程序 ---')
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
            if not messagebox.askyesno('程式中', '推導仍在執行，要停止並關閉嗎？'):
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
            self._append('\n=== 結束（exit=%d）===' % rc)
            self.lbl_stage.configure(
                text=('完成 ✓' if rc == 0 else '失敗（exit=%d）' % rc))
            if rc == 0:
                self.progress['value'] = 100
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
            self.lbl_stage.configure(text='[%d/%d] %s' % (idx, total, name))
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


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    root = tk.Tk()
    app = App(root)
    if '--self-test' in argv:
        root.after(800, root.destroy)
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
