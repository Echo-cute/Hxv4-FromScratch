"""progress.py -- 執行時的 log 顯示與進度回報。

* 同時輸出到 console 與 log 檔（``--log-file`` 或自動 ``work/logs/``）。
* 每行都有時間戳；長迴圈用 :meth:`progress` 節流輸出（預設 0.8 秒一次）。
* 不依賴任何第三方套件。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


def fmt_seconds(sec: float) -> str:
    sec = max(0.0, float(sec))
    if sec < 60:
        return '%.1fs' % sec
    minutes, s = divmod(sec, 60)
    if minutes < 60:
        return '%dm%02.0fs' % (minutes, s)
    hours, minutes = divmod(minutes, 60)
    return '%dh%02dm' % (hours, minutes)


class Reporter:
    def __init__(self, log_file: str | Path | None = None,
                 verbose: bool = True, prefix_time: bool = True) -> None:
        self.verbose = verbose
        self.prefix_time = prefix_time
        self.t0 = time.time()
        self.log_file = str(log_file) if log_file else None
        self._fh = None
        self._last_progress = 0.0
        if log_file:
            path = Path(log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open('a', encoding='utf-8')
            self._fh.write('\n===== %s =====\n' %
                           time.strftime('%Y-%m-%d %H:%M:%S'))
            self._fh.flush()

    def log(self, *args) -> None:
        msg = ' '.join(str(a) for a in args)
        line = ('[%s] %s' % (time.strftime('%H:%M:%S'), msg)
                if self.prefix_time else msg)
        if self.verbose:
            print(line, flush=True)
        if self._fh:
            self._fh.write(line + '\n')
            self._fh.flush()

    def stage(self, idx: int, total: int, name: str) -> None:
        self.log('===== [%d/%d] %s =====' % (idx, total, name))

    def progress(self, label: str, done: int, total: int | None = None,
                 *, force: bool = False, interval: float = 0.8,
                 extra: str = '') -> None:
        now = time.time()
        if not force and now - self._last_progress < interval:
            return
        self._last_progress = now
        elapsed = now - self.t0
        rate = done / elapsed if elapsed > 0 else 0.0
        parts = ['%s: %s' % (label, format(done, ','))]
        if total:
            pct = 100.0 * done / total
            parts.append('%.1f%%' % pct)
            parts.append('(%s/%s)' % (format(done, ','), format(total, ',')))
        parts.append('%s' % fmt_seconds(elapsed))
        if rate >= 1:
            parts.append('%s/s' % format(int(rate), ','))
        if extra:
            parts.append(extra)
        self.log('  ' + ' '.join(parts))

    def close(self) -> None:
        if self._fh:
            self._fh.write('===== done: %s =====\n' % fmt_seconds(
                time.time() - self.t0))
            self._fh.close()
            self._fh = None
