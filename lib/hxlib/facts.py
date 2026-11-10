"""facts.py -- 檔名候選（Fact）的共通資料結構與落地格式。

設計原則
--------
* **解析器只負責「看到什麼」**：PSB / TJS / 文字檔解析器把「遊戲資料裡實際存在的
  字串或欄位值」丟成 Fact，並附上可回溯的 provenance（哪個 entry、哪個欄位）。
* **組合、規則、驗證集中在 pipeline / rules / verify**：解析器不做 hash、
  不猜副檔名、不掃數字，避免各模組各自亂長規則。
* Fact 的 value 必須是**遊戲資料中的原始字串**（UTF-8 / UTF-16 解出來的
  Unicode 原樣），大小寫、全形半形、符號都不可以自行轉換，因為遊戲是拿
  這個字串的 UTF-16LE bytes 去算 BLAKE2s 的。

三種 kind
---------
NAME      已是完整檔名（含副檔名，例如 ``ev101aa.tlg``、``bgm01.opus.sli``）。
DIR       目錄 entry（尾端一定是 ``/``，例如 ``system/``、``locale/cn/``）。
FRAGMENT  名稱片段（例如 PSB 的 ``storage="ev101aa"``、PBD 的 ``filename:'xxx'``）。
          之後由 rules.py 用「有遊戲程式碼/資料證據的副檔名規則」組成 NAME。

（TEMPLATE 不放進 Fact：組合邏輯屬 logic.py，它的輸出應該已經是具體 NAME。）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable, Iterator

KIND_NAME = 'NAME'
KIND_DIR = 'DIR'
KIND_FRAGMENT = 'FRAGMENT'
KINDS = (KIND_NAME, KIND_DIR, KIND_FRAGMENT)

# 目錄 entry 合法字元：Windows 檔名不允許的字元不可能出現在遊戲檔名裡，
# 但遊戲用的是 UTF-16 且允許符號（→ ≪ † ★ 等），這裡只擋控制字元與 NUL。
_BAD_CHARS = set('\x00\r\n\t')


def is_plausible_value(value: str, *, kind: str = KIND_NAME) -> bool:
    """基本合理性檢查（不是白名單過濾：只排除不可能是檔名的東西）。"""
    if not value or len(value) > 255:
        return False
    if any(ch in _BAD_CHARS for ch in value):
        return False
    if kind == KIND_DIR:
        return value.endswith('/') and '//' not in value and not value.startswith('/')
    if value.endswith('/'):
        return False
    if value in ('.', '..'):
        return False
    return True


@dataclass(frozen=True)
class Fact:
    value: str
    kind: str = KIND_NAME
    source: str = ''
    note: str = ''
    # context：值的「語意位置」，規則（rules.py）靠它決定要不要加副檔名，例如
    # ``kag:image.storage``、``psb:*.storage``、``csv:charvoice.col1``、``stand:filename``。
    context: str = ''

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError('bad Fact.kind: %r' % (self.kind,))

    def key(self) -> tuple[str, str]:
        return (self.kind, self.value)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def iter_jsonl(path: str | Path) -> Iterator[Fact]:
    path = Path(path)
    if not path.is_file():
        return
    with path.open(encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                yield Fact(
                    value=obj['value'],
                    kind=obj.get('kind', KIND_NAME),
                    source=obj.get('source', ''),
                    note=obj.get('note', ''),
                    context=obj.get('context', ''),
                )
            except (KeyError, ValueError):
                continue


class FactWriter:
    """把 Fact 寫成 JSONL；同一 (kind, value) 只留第一筆（provenance 也保留第一筆）。

    source 內可帶 ``|`` 分隔的多來源，但為了檔案大小，重複值不再追蹤。
    """

    def __init__(self, path: str | Path, *, include_fragments: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open('w', encoding='utf-8')
        self._seen: set[tuple[str, str]] = set()
        self._count = 0
        self.include_fragments = include_fragments
        self.skipped = 0

    def add(self, fact: Fact) -> bool:
        if not self.include_fragments and fact.kind == KIND_FRAGMENT:
            return False
        key = fact.key()
        if key in self._seen:
            return False
        if not is_plausible_value(fact.value, kind=fact.kind):
            self.skipped += 1
            return False
        self._seen.add(key)
        self._fh.write(fact.to_json() + '\n')
        self._count += 1
        return True

    def add_many(self, facts: Iterable[Fact]) -> int:
        n = 0
        for fact in facts:
            n += self.add(fact)
        return n

    def close(self) -> int:
        self._fh.close()
        return self._count

    @property
    def count(self) -> int:
        return self._count

    def __enter__(self) -> 'FactWriter':
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def load_facts(paths: Iterable[str | Path]) -> list[Fact]:
    out: list[Fact] = []
    seen: set[tuple[str, str]] = set()
    for path in paths:
        for fact in iter_jsonl(path):
            key = fact.key()
            if key in seen:
                continue
            seen.add(key)
            out.append(fact)
    return out
