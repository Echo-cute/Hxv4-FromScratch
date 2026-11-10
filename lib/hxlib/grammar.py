"""grammar.py -- 從「已命中的名字」歸納命名文法，再做家族補齊。

設計原則（跨遊戲通用，不寫死任何遊戲的檔名、角色、字型或目錄）：

* **只從遊戲自己的名字學習**：輸入是已經被 hash 表驗證過的 name 集合。
* **樣式簽名**：把名字拆成 literal 與有限幾種槽位
  （數字、單字母、字母串、季節字、locale 後綴、引擎 wrapper 前綴），
  簽名相同且樣本 >= 2 的名字才被視為一條「文法」。
* **生成只動槽位**：數字補洞/向前後延伸、字母 a-z、字母串窮舉（長度 <= 3）、
  季節、locale、以及遊戲自己出現過的 wrapper 前綴 × 已命中的基底名。
* **babysitting 由 hash oracle 負責**：本模組只產生候選，
  ``pipeline`` 仍用 BLAKE2s 對表驗證，沒中的直接丟棄，因此不會產生假名。

引擎層（非遊戲層）的固定機制另外放在 ``hxlib.engine``，例如
PackinOne digest／layer 命名；本模組不應該出現任何引擎常數。
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Iterator

SEASONS = ('春', '夏', '秋', '冬')
_LOCALES = ('jp', 'cn', 'en', 'tw')

# 槽位符號
_SLOT_NUM = '\x00'      # 數字串
_SLOT_LETTER = '\x01'   # 單一小寫字母
_SLOT_LETTERS = '\x02'  # 小寫字母串（長度保留）
_SLOT_SEASON = '\x03'   # 春/夏/秋/冬
_SLOT_LOCALE = '\x04'   # _jp/_cn/_en/_tw


def _split_ext(name: str) -> tuple[str, str]:
    stem, ext = os.path.splitext(name)
    if not ext:
        # 支援 .ogg.sli 這類雙副檔名
        stem2, ext2 = os.path.splitext(stem)
        if ext2 and ext in ('.sli', '.csv', '.vol'):
            return stem2, ext2 + ext
    return stem, ext


def _signature(stem: str) -> tuple[str, list[tuple[int, int, str]]]:
    """回傳 (簽名, [(slot_start, slot_len, slot_kind), ...])。"""
    slots: list[tuple[int, int, str]] = []
    out: list[str] = []
    i = 0
    n = len(stem)
    while i < n:
        ch = stem[i]
        # locale 後綴：_jp/_cn/_en/_tw
        if ch == '_':
            m = re.match(r'_(jp|cn|en|tw)(?=$|[_.])', stem[i:])
            if m:
                slots.append((len(''.join(out)), len(m.group(0)),
                              _SLOT_LOCALE))
                out.append(_SLOT_LOCALE)
                i += len(m.group(0))
                continue
        # 季節字
        if ch in SEASONS:
            slots.append((len(''.join(out)), 1, _SLOT_SEASON))
            out.append(_SLOT_SEASON)
            i += 1
            continue
        # 數字串
        if ch.isdigit():
            j = i
            while j < n and stem[j].isdigit():
                j += 1
            slots.append((len(''.join(out)), j - i, _SLOT_NUM))
            out.append(_SLOT_NUM)
            i = j
            continue
        # 小寫字母串（前後需為分隔符或字串邊界；避免把一般英文單字拆掉）
        if ch.islower() and ch.isascii():
            j = i
            while j < n and stem[j].islower() and stem[j].isascii():
                j += 1
            run = stem[i:j]
            prev = stem[i - 1] if i > 0 else ''
            nxt = stem[j] if j < n else ''
            def _delim(ch: str) -> bool:
                return (not ch) or (not ch.isascii()) or ch in '_-.／/（）()[]【】'
            boundary = _delim(prev) and _delim(nxt)
            if boundary and len(run) <= 3:
                slots.append((len(''.join(out)), len(run), _SLOT_LETTERS))
                out.append(_SLOT_LETTERS)
                i = j
                continue
        out.append(ch)
        i += 1
    return ''.join(out), slots


def _slot_values(stem: str, slots) -> dict[str, list[str]]:
    vals: dict[str, list[str]] = {_SLOT_NUM: [], _SLOT_LETTERS: [],
                                  _SLOT_SEASON: [], _SLOT_LOCALE: []}
    for start, length, kind in slots:
        vals[kind].append(stem[start:start + length])
    return vals


def _letter_combos(length: int, limit: int = 40000) -> Iterator[str]:
    if length <= 0 or length > 3:
        return
    total = 26 ** length
    if total > limit:
        return
    if length == 1:
        for a in 'abcdefghijklmnopqrstuvwxyz':
            yield a
        return
    for a in 'abcdefghijklmnopqrstuvwxyz':
        for b in 'abcdefghijklmnopqrstuvwxyz':
            if length == 2:
                yield a + b
            else:
                for c in 'abcdefghijklmnopqrstuvwxyz':
                    yield a + b + c


def _render(sig: str, slots, values: dict[str, str]) -> str:
    """把簽名 + 槽位值渲染回 stem。"""
    parts: list[str] = []
    idx = 0
    for start, length, kind in slots:
        # signature 的開始位置是「已輸出字元」位置，換回 slot 在 sig 的 index
        pass
    # 直接逐字元走 signature，遇到槽位符號就填值（槽位順序即出現順序）
    order = [k for _, _, k in slots]
    oi = 0
    for ch in sig:
        if ch in (_SLOT_NUM, _SLOT_LETTER, _SLOT_LETTERS, _SLOT_SEASON,
                  _SLOT_LOCALE):
            kind = order[oi] if oi < len(order) else ch
            oi += 1
            parts.append(values.get(kind, ''))
        else:
            parts.append(ch)
    return ''.join(parts)


def family_candidates(names: Iterable[str], *, min_instances: int = 2,
                      max_candidates: int = 25_000_000) -> Iterator[str]:
    """由已命中名字歸納文法，產生家族補齊候選（不含驗證）。"""
    names = [n for n in names if n]
    known = set(names)
    groups: dict[tuple[str, str], list[str]] = {}
    ext_pool: set[str] = set()
    wrapper_prefixes: dict[str, int] = {}
    dunder_tails: set[str] = set()
    for name in names:
        stem, ext = _split_ext(name)
        ext_pool.add(ext)
        sig, slots = _signature(stem)
        groups.setdefault((sig, ext), []).append(stem)
        if '_' in stem:
            head = stem.split('_', 1)[0]
            if 1 < len(head) <= 16 and head.isalpha():
                wrapper_prefixes[head] = wrapper_prefixes.get(head, 0) + 1
        m = re.search(r'__(pack|bg\d+|frame|anim)$', stem)
        if m:
            dunder_tails.add(m.group(1))
    emitted = 0

    def emit(cand: str):
        nonlocal emitted
        if cand and cand not in known and '//' not in cand:
            emitted += 1
            yield cand

    # 2) locale 後綴機制（引擎層語系檔名）：在已知名字的 stem 上加/換 locale
    for name in names:
        stem, ext = _split_ext(name)
        m = re.search(r'_(jp|cn|en|tw)$', stem)
        if m:
            base = stem[:m.start()]
            for loc in _LOCALES:
                cand = base + '_' + loc + ext
                if cand not in known:
                    emitted += 1
                    yield cand
        else:
            for loc in _LOCALES:
                cand = stem + '_' + loc + ext
                if cand not in known:
                    emitted += 1
                    yield cand

    # 3) wrapper 前綴（遊戲自己出現過的 prefix token × 已命中基底名）
    if wrapper_prefixes:
        # 只取看起來像 wrapper 的前綴（出現 >= 2 次、長度 2..10、全小寫英文）
        prefixes = [p for p, c in wrapper_prefixes.items()
                    if c >= 2 and 2 <= len(p) <= 10 and p.isalpha()]
        bases = set()
        for name in names:
            stem, _ = _split_ext(name)
            if '_' not in stem:
                continue
            for p in prefixes:
                pref = p + '_'
                if stem.startswith(pref) and len(stem) > len(pref):
                    bases.add(stem[len(pref):])
        for base in list(bases)[:5000]:
            for ext in ('.png', '.jpg', '.tlg', '.pbd', '.tjs'):
                for p in prefixes:
                    cand = p + '_' + base + ext
                    if cand not in known:
                        emitted += 1
                        yield cand

    # 4) `__pack`/`__bgN` 圖層包裝名：拆出基底名，補齊資料表/圖片
    dunder_names = set()
    for name in names:
        stem, ext = _split_ext(name)
        m = re.search(r'__(pack|bg\d+|frame|anim)$', stem)
        if not m:
            continue
        base = stem[:m.start()]
        dunder_names.add(base)
        for e in ext_pool:
            if e in ('.tlg', '.png', '.jpg', '.pbd', '.tjs', '.csv', '.ini'):
                cand = base + e
                if cand not in known:
                    emitted += 1
                    yield cand
        for loc in _LOCALES:
            for e in ('.pbd', '.tjs', '.png', '.jpg', '.tlg'):
                cand = base + '_' + loc + e
                if cand not in known:
                    emitted += 1
                    yield cand
    # 5) 反向：已命中的基底名 → 生成 wrapper 圖層名（__pack/__bg0 等）
    for base in list(dunder_names)[:20000]:
        for tail in dunder_tails:
            for e in ('.tlg', '.png', '.jpg'):
                cand = base + '__' + tail + e
                if cand not in known:
                    emitted += 1
                    yield cand

    # 1) 同簽名家族（最後跑：候選量大，避免吃掉前面精確機制的名額）
    # 1) 同簽名家族：槽位替換（數字補洞/延伸、字母、字母串、季節、locale）
    for (sig, ext), stems in groups.items():
        if len(stems) < min_instances:
            continue
        group_emitted = 0
        allowance = 4000 if emitted < 18_000_000 else 200
        # 小型的字母串家族（如 chaticon_xxx）要能窮舉到 26^3
        run_lens = {len(v) for _stem, slots, vals in
                    [(x[0], x[1], x[2]) for x in
                     ((st, _signature(st)[1], _slot_values(st, _signature(st)[1]))
                      for st in stems)] for v in vals.get(_SLOT_LETTERS, [])}
        if run_lens and max(run_lens) <= 3 and len(stems) <= 8:
            allowance = 30000
        # 解析每個 stem 的槽位值
        parsed = []
        for stem in stems:
            sig2, slots = _signature(stem)
            if sig2 != sig:
                continue
            parsed.append((stem, slots, _slot_values(stem, slots)))
        if len(parsed) < min_instances:
            continue
        kinds = {k for _, _, vals in parsed for k, v in vals.items() if v}
        # 數字槽
        if _SLOT_NUM in kinds and group_emitted < allowance:
            nums = []
            for _, _, vals in parsed:
                for v in vals.get(_SLOT_NUM, []):
                    try:
                        nums.append(int(v))
                    except ValueError:
                        pass
            if nums:
                lo, hi = min(nums), max(nums)
                span = hi - lo
                length = max(len(v) for _, _, vals in parsed
                             for v in vals.get(_SLOT_NUM, []))
                start = max(0, lo - 8)
                end = min(hi + 400, start + 12000)
                other_slots = kinds - {_SLOT_NUM}
                # 其他槽位若所有樣本都同值（例如固定前綴字母），只需一個代表
                other_fixed = True
                for kind in other_slots:
                    vals_set = {v for _st, _sl, vv in parsed
                                for v in vv.get(kind, [])}
                    if len(vals_set) > 1:
                        other_fixed = False
                reps = parsed[:1] if (not other_slots or other_fixed) else parsed[:32]
                for v in range(start, end + 1):
                    if group_emitted >= allowance:
                        break
                    s = str(v).zfill(length) if length > 1 else str(v)
                    for stem, slots, vals in reps:
                        values = {k: (vals[k][0] if vals.get(k) else '')
                                  for k in vals}
                        values[_SLOT_NUM] = s
                        cand = _render(sig, slots, values) + ext
                        if emitted >= max_candidates:
                            return
                        if cand not in known:
                            emitted += 1
                            group_emitted += 1
                            yield cand
        # 字母槽
        if _SLOT_LETTERS in kinds and group_emitted < allowance:
            seen_runs = {len(v) for _, _, vals in parsed
                         for v in vals.get(_SLOT_LETTERS, [])}
            for stem, slots, vals in parsed[:64]:
                values = {k: (vals[k][0] if vals.get(k) else '') for k in vals}
                for run_len in seen_runs:
                    if run_len > 3:
                        continue
                    for combo in _letter_combos(run_len):
                        if group_emitted >= allowance:
                            break
                        values[_SLOT_LETTERS] = combo
                        for season in ('',) + SEASONS:
                            if _SLOT_SEASON in kinds:
                                values[_SLOT_SEASON] = season
                            cand = _render(sig, slots, values) + ext
                            if emitted >= max_candidates:
                                return
                            if cand not in known:
                                emitted += 1
                                group_emitted += 1
                                yield cand
        # 季節槽（名字本身有季節字者）
        if _SLOT_SEASON in kinds and _SLOT_LETTERS not in kinds:
            for stem, slots, vals in parsed[:64]:
                values = {k: (vals[k][0] if vals.get(k) else '') for k in vals}
                for season in SEASONS:
                    values[_SLOT_SEASON] = season
                    cand = _render(sig, slots, values) + ext
                    if emitted >= max_candidates:
                        return
                    if cand not in known:
                        emitted += 1
                        group_emitted += 1
                        yield cand

