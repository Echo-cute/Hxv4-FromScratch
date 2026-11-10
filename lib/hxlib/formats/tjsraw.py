"""TJS 容器「原始字串」抽取（補 logic.tjs 的語意過濾）。

``formats.tjs`` 只把「看起來像檔名/路徑」的字串變成 Fact（6/檔案），
但引擎的設定表（``SystemConfig`` 預設值、``SetSystemConfigDefaults``）常把
純 stem 存成字串常量，檔名是執行時才接上副檔名（例如
``sqscrMaskFile: "sqscrmask"`` → ``sqscrmask.png``）。

這個模組把 TJS2100 的 DATA 字串池、TJS/ns0 的 variant 字串全部倒出來，
只做基本雜訊過濾（長度、控制字元、程式符號），其餘交給 rules 的
``engine_ext_fill`` 逐一接副檔名再用 BLAKE2s 驗證 —— 不會產生假名字，
候選量有界（每個字串 × 引擎副檔名清單）。
"""
from __future__ import annotations

import re
from typing import Iterator

from ..facts import Fact, KIND_FRAGMENT

KINDS = ('tjs',)

_TJS_MAGIC = b'TJS2100\x00'
_NS_MAGIC = b'TJS/ns0\x00'
_NUM_RE = re.compile(r'^[\d.\-+eE]+$')
_BAD = set('\x00\r\n\t')


def _plausible(value: str) -> bool:
    if not value or len(value) < 2 or len(value) > 120:
        return False
    if any(ch in _BAD for ch in value):
        return False
    if any(ord(ch) < 0x20 for ch in value):
        return False
    if value.startswith(('//', '/*', '*/', '#', '@', ';', '$', '%')):
        return False
    if value.strip() != value:
        return False
    if _NUM_RE.match(value):
        return False
    # 純大寫/全大寫或含程式符號的識別字多半是常數/程式碼；小寫識別字則常是
    # 設定表的檔名 stem（例 charlist / imagediffmap / sqscrmask），保留。
    if re.fullmatch(r'[A-Z][A-Za-z0-9]*', value):
        return False
    if re.fullmatch(r'[A-Z_][A-Z0-9_]*', value):
        return False
    return True


def _strings(data: bytes) -> list[str]:
    from . import tjs as tjsmod
    try:
        return tjsmod.all_strings(data)
    except Exception:
        return []


def iter_facts(entry, data: bytes) -> Iterator[Fact]:
    source = '%s#%s' % (getattr(entry, 'archive', '?'),
                        getattr(entry, 'index', '?'))
    seen: set[str] = set()
    for s in _strings(data):
        if s in seen or not _plausible(s):
            continue
        seen.add(s)
        yield Fact(value=s, kind=KIND_FRAGMENT, source=source,
                   note='TJS 容器字串常量（設定表 stem 可能在執行時接副檔名）',
                   context='tjs:const')
