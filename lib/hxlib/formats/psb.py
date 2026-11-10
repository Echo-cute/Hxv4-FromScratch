"""psb.py -- PSB（M2 / KiriKiri PSB v3/v4）解析 + 檔名 Fact 抽取。

PSB 格式（實測 tenshi 526 個 PSB 全數可解析）
=============================================

::

    file  = b'PSB\\0' + header(version, flags) + 8 個 u32 區段位移
    header: magic[4] version u16 flags u16  offset[8] u32
            offset[1] = name b-tree 陣列（3 個 psb_array 連續排列）
            offset[2] = string offset 陣列
            offset[3] = string data pool（NUL 結尾的 UTF-8 字串）
            offset[4]/[5]/[6] = resource chunk offsets/lengths/data
            offset[7] = root object tree（tag 0x21）

值編碼是 tagged binary：

* 0x01 null、0x02/0x03 bool、0x04..0x0C 有號整數（寬度 tag-4）
* 0x0D..0x14 packed integer array（count_type/entry_type = 0x0C+寬度）
* 0x15..0x18 string 參照（index 寬度 tag-0x14）
* 0x19..0x1C resource 參照（index 寬度 tag-0x18）→ ``{'$res': index}``
* 0x1D 0.0、0x1E float32、0x1F float64
* 0x20 list（tag 後接 offset array，值相對 array 結尾）
* 0x21 dict（tag 後接 name-index array + offset array）

字串池用 **UTF-8** 編碼（實測日文／符號都直接以 UTF-8 存在 pool 裡）。
舊版工具誤用 ASCII 解碼，把 ``→→走る／学校／靴``、``†吸血``、``stamp_ありがとう``
這類名字全毀掉，是本模組要修掉的重點。

Fact 抽取原則
=============

* 走訪整棵樹的每個字串葉子（含陣列／物件的巢狀路徑），**一個都不漏**。
* 值結尾是已知副檔名（``.tlg``/``.ogg``/...）→ ``NAME``。
* 值結尾是 ``/`` → ``DIR``。
* 值本身沒有副檔名，但所在的欄位名是檔名語意欄位（``storage``/``file``/
  ``filename``/``name``/``refile``/``sename``/``bgm``/``voice`` …）→ ``FRAGMENT``。
* 場景 PSB（root 有 ``scenes``）的 root ``name`` 是編譯後的 KAG 場景名，
  實際 entry 名是 ``<name>.scn``（已用 hash 表驗證：``x_e13_3.ks`` → entry
  確實是 ``x_e13_3.ks.scn``）→ 丟 ``FRAGMENT``，由 rules.py 加 ``.scn``。
* ``source`` = ``<archive>#<entry_idx>:<json path>``；``context`` = ``psb:<欄位語意>``。
* 不做 hash、不組合、不猜副檔名；解析失敗時退回 raw UTF-8/UTF-16LE 字串掃描，
  保證「單檔失敗不會讓 pipeline 漏掉整個 entry、也不會 crash」。
"""
from __future__ import annotations

import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

HERE = Path(__file__).resolve().parent
LIB = HERE.parent.parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from hxlib.facts import Fact, KIND_NAME, KIND_DIR, KIND_FRAGMENT  # noqa: E402

KINDS = ('psb',)

PSB_MAGIC = b'PSB\0'
_HEADER_MIN = 40

# 已知「像檔名」的副檔名（任務給的清單 + 實測資料裡出現的媒體副檔名）。
# 這只是「這串字看起來是不是檔名」的快速判斷，不是驗證用的白名單。
KNOWN_EXTS = (
    '.tlg', '.png', '.ogg', '.opus', '.psb', '.scn', '.pbd', '.tjs', '.ks',
    '.mtn', '.asd', '.tft', '.otf', '.ttf', '.mp4', '.wmv', '.sli', '.toml',
    '.ini', '.csv', '.txt', '.stage', '.stand', '.sinfo',
    # 實測資料中額外出現的媒體型態
    '.jpg', '.jpeg', '.wav', '.pimg', '.func', '.dll', '.ns0',
)
_EXT_RE = re.compile(
    r'\.(?:' + '|'.join(e[1:] for e in KNOWN_EXTS) + r')$', re.IGNORECASE)

# 「這個欄位的值可能是檔名片段」的欄位名（引擎之後會自己接副檔名/路徑）。
NAMEISH_KEYS = frozenset({
    'storage', 'storage2', 'file', 'filename', 'filepath', 'path',
    'name', 'refile', 'sename', 'bgm', 'bgmname', 'voice', 'voicename',
    'se', 'se_name', 'sound', 'soundname', 'music', 'movie', 'image',
    'imagefile', 'thumb', 'thumbnail', 'icon', 'face', 'chara', 'char',
    'layer', 'mask', 'base', 'target', 'src', 'resource', 'resname',
    'scn', 'scenario', 'script', 'tjs', 'font', 'back', 'fore',
})
DIR_KEYS = frozenset({'dir', 'directory', 'folder', 'autopath', 'autoPath'})

# 純 hash 字串（16/32/64 hex）是內容雜湊，不是檔名，不當 Fact。
_HEX_RE = re.compile(r'^[0-9a-fA-F]{16}$|^[0-9a-fA-F]{32}$|^[0-9a-fA-F]{64}$')


class PSBError(Exception):
    """PSB 解析錯誤。"""


class PSBMagicError(PSBError):
    pass


class PSBVersionError(PSBError):
    pass


def looks_like_filename(value: str) -> bool:
    """值本身看起來就是完整檔名（有已知副檔名、長度合理）。"""
    if not value or len(value) > 255:
        return False
    if '\x00' in value or '\n' in value or '\r' in value:
        return False
    return bool(_EXT_RE.search(value))


def looks_like_dir(value: str) -> bool:
    return (len(value) > 1 and value.endswith('/') and '//' not in value
            and not value.startswith('/'))


def is_hex_hash(value: str) -> bool:
    return bool(_HEX_RE.match(value))


# ---------------------------------------------------------------------------
# psb_array / varint
# ---------------------------------------------------------------------------
def _read_uint(data: bytes, offset: int, size: int) -> int:
    if size < 0 or size > 8:
        raise PSBError('unsupported integer width %d at 0x%X' % (size, offset))
    if offset < 0 or offset + size > len(data):
        raise PSBError('integer at 0x%X exceeds file size' % offset)
    return int.from_bytes(data[offset:offset + size], 'little')


@dataclass
class PSBArray:
    """krkrpsb ``psb_array_t``：count/entry 各自帶寬度的 packed 整數陣列。"""
    offset: int
    end: int
    count: int
    values: list[int]
    entry_width: int


# ---------------------------------------------------------------------------
# PSBHeader
# ---------------------------------------------------------------------------
@dataclass
class PSBHeader:
    version: int
    flags: int
    offsets: tuple[int, ...]          # 8 個 u32，語意見下面的 property
    header_size: int

    @property
    def offset_names(self) -> int:
        return self.offsets[1]

    @property
    def offset_strings(self) -> int:
        return self.offsets[2]

    @property
    def offset_strings_data(self) -> int:
        return self.offsets[3]

    @property
    def offset_chunk_offsets(self) -> int:
        return self.offsets[4]

    @property
    def offset_chunk_lengths(self) -> int:
        return self.offsets[5]

    @property
    def offset_chunk_data(self) -> int:
        return self.offsets[6]

    @property
    def offset_entries(self) -> int:
        return self.offsets[7]


def parse_header(data: bytes) -> PSBHeader:
    if len(data) < _HEADER_MIN:
        raise PSBError('file too small for PSB header: %d bytes' % len(data))
    if data[:4] != PSB_MAGIC:
        raise PSBMagicError('bad PSB magic: %r' % (data[:4],))
    version, flags = struct.unpack_from('<HH', data, 4)
    if version not in (2, 3, 4):
        raise PSBVersionError('unsupported PSB version %d' % version)
    header_size = 40 if version in (2, 3) else 56
    # 8 個 u32 從 offset 8 開始（v4 header 較長：offset[0]/[1] 直接指到 header 之後）
    offsets = struct.unpack_from('<8I', data, 8)
    if offsets[7] >= len(data):
        raise PSBError('entries offset 0x%X outside file (size 0x%X)'
                       % (offsets[7], len(data)))
    return PSBHeader(version=version, flags=flags, offsets=tuple(offsets),
                     header_size=header_size)


# ---------------------------------------------------------------------------
# PSBReader
# ---------------------------------------------------------------------------
class PSBReader:
    """把 PSB bytes 解成 Python 樹（dict/list/int/float/str/bool/None）。

    設計成自帶完整實作（不依賴 vendor 的 psb_parser），且對壞檔只丟
    :class:`PSBError`，讓呼叫端能 catch 後退回 raw string 掃描。
    """

    def __init__(self, data: bytes):
        if len(data) < _HEADER_MIN:
            raise PSBError('file too small: %d bytes' % len(data))
        self._data = data
        self.header = parse_header(data)
        self._names: list[str] | None = None
        self._name_arrays: tuple[PSBArray, PSBArray, PSBArray] | None = None
        self._string_offsets: PSBArray | None = None
        self._strings: list[str] | None = None
        self._string_cache: dict[int, str] = {}
        self._name_cache: dict[int, str] = {}
        self._depth_guard = 0
        self.resource_count = 0
        try:
            res = self.read_psb_array(self.header.offset_chunk_offsets)
            self.resource_count = res.count
        except PSBError:
            self.resource_count = 0

    # -- file: path 入口（方便獨立測試）
    @classmethod
    def from_path(cls, path: str | Path) -> 'PSBReader':
        return cls(Path(path).read_bytes())

    # ------------------------------------------------------------- arrays
    def _read_array(self, offset: int) -> tuple[list[int], int]:
        """讀 packed 整數陣列，回傳 ``(values, end_offset)``（速度熱點）。"""
        data = self._data
        size = len(data)
        if offset < 0 or offset >= size:
            raise PSBError('array offset 0x%X outside file' % offset)
        cursor = offset
        count_type = data[cursor]
        cursor += 1
        count_width = count_type - 0x0C
        if count_width < 1 or count_width > 8:
            raise PSBError('bad array count type 0x%02X at 0x%X'
                           % (count_type, offset))
        count = _read_uint(data, cursor, count_width)
        cursor += count_width
        if cursor >= size:
            raise PSBError('array at 0x%X truncated' % offset)
        entry_width = data[cursor] - 0x0C
        cursor += 1
        if entry_width < 1 or entry_width > 8:
            raise PSBError('bad array entry type 0x%02X at 0x%X'
                           % (data[cursor - 1], cursor - 1))
        need = cursor + count * entry_width
        if count > 0x4000000:
            raise PSBError('implausible array count %d at 0x%X' % (count, offset))
        if need > size:
            raise PSBError('array at 0x%X exceeds file size' % offset)
        if count == 0:
            values: list[int] = []
        elif entry_width == 4:
            values = list(struct.unpack_from('<%dI' % count, data, cursor))
        elif entry_width == 2:
            values = list(struct.unpack_from('<%dH' % count, data, cursor))
        elif entry_width == 1:
            values = list(data[cursor:need])
        elif entry_width == 8:
            values = list(struct.unpack_from('<%dQ' % count, data, cursor))
        else:
            values = [int.from_bytes(data[cursor + i * entry_width:
                                         cursor + (i + 1) * entry_width], 'little')
                      for i in range(count)]
        return values, need

    def read_psb_array(self, offset: int) -> PSBArray:
        saved = offset
        values, end = self._read_array(offset)
        return PSBArray(offset=saved, end=end, count=len(values), values=values,
                        entry_width=0)

    # --------------------------------------------------------- name btree
    def read_name_arrays(self) -> tuple[PSBArray, PSBArray, PSBArray]:
        if self._name_arrays is None:
            a1 = self.read_psb_array(self.header.offset_names)
            a2 = self.read_psb_array(a1.end)
            a3 = self.read_psb_array(a2.end)
            self._name_arrays = (a1, a2, a3)
        return self._name_arrays

    def get_name(self, index: int) -> str:
        cached = self._name_cache.get(index)
        if cached is not None:
            return cached
        str1, str2, str3 = self.read_name_arrays()
        if index < 0 or index >= len(str3.values):
            raise PSBError('name index %d outside table (%d)'
                           % (index, len(str3.values)))
        chars: list[int] = []
        a = str3.values[index]
        b = str2.values[a]
        while True:
            c = str2.values[b]
            d = str1.values[c]
            chars.append(b - d)
            b = c
            if b == 0:
                break
        name = bytes(reversed(chars)).decode('ascii', errors='replace')
        self._name_cache[index] = name
        return name

    def read_names(self) -> list[str]:
        if self._names is None:
            str3 = self.read_name_arrays()[2]
            self._names = [self.get_name(i) for i in range(str3.count)]
        return self._names

    # --------------------------------------------------------- string pool
    def read_string_offsets(self) -> PSBArray:
        if self._string_offsets is None:
            self._string_offsets = self.read_psb_array(self.header.offset_strings)
        return self._string_offsets

    def get_string(self, index: int) -> str:
        cached = self._string_cache.get(index)
        if cached is not None:
            return cached
        arr = self.read_string_offsets()
        if index < 0 or index >= arr.count:
            raise PSBError('string index %d outside table (%d)'
                           % (index, arr.count))
        start = self.header.offset_strings_data + arr.values[index]
        if start < 0 or start >= len(self._data):
            raise PSBError('string offset 0x%X outside file' % start)
        end = self._data.find(0, start)
        if end < 0:
            raise PSBError('unterminated string at 0x%X' % start)
        # PSB 字串池是 UTF-8（日文、記號都是）；失敗時保留 replacement char。
        text = self._data[start:end].decode('utf-8', errors='replace')
        self._string_cache[index] = text
        return text

    def read_string_table(self) -> list[str]:
        if self._strings is None:
            arr = self.read_string_offsets()
            self._strings = [self.get_string(i) for i in range(arr.count)]
        return self._strings

    # ------------------------------------------------------------- values
    def parse_value(self, offset: int) -> tuple[Any, int]:
        """回傳 ``(值, 消耗後 offset)``。"""
        return self._parse_value(offset, 0)

    def _parse_value(self, offset: int, depth: int) -> tuple[Any, int]:
        data = self._data
        if offset < 0 or offset >= len(data):
            raise PSBError('value offset 0x%X outside file' % offset)
        tag = data[offset]
        cursor = offset + 1

        # packed integer array 0x0D..0x14（出現頻率最高，最先判斷）
        if 0x0D <= tag <= 0x14:
            values, end = self._read_array(offset)
            return values, end
        # string reference 0x15..0x18
        if 0x15 <= tag <= 0x18:
            size = tag - 0x14
            index = _read_uint(data, cursor, size)
            return self.get_string(index), cursor + size
        # 有號整數 0x04..0x0C（0x04 = 固定 0）
        if 0x04 <= tag <= 0x0C:
            size = tag - 0x04
            if size == 0:
                return 0, cursor
            raw = _read_uint(data, cursor, size)
            cursor += size
            sign = 1 << (size * 8 - 1)
            if raw & sign:
                raw -= 1 << (size * 8)
            return raw, cursor
        # null / bool
        if tag == 0x01:
            return None, cursor
        if tag == 0x02:
            return False, cursor
        if tag == 0x03:
            return True, cursor
        # resource reference 0x19..0x1C
        if 0x19 <= tag <= 0x1C:
            size = tag - 0x18
            index = _read_uint(data, cursor, size)
            return {'$res': index}, cursor + size
        # 數字 0x1D..0x1F
        if tag == 0x1D:
            return 0.0, cursor
        if tag == 0x1E:
            if cursor + 4 > len(data):
                raise PSBError('float32 at 0x%X truncated' % offset)
            return struct.unpack_from('<f', data, cursor)[0], cursor + 4
        if tag == 0x1F:
            if cursor + 8 > len(data):
                raise PSBError('float64 at 0x%X truncated' % offset)
            return struct.unpack_from('<d', data, cursor)[0], cursor + 8
        # list
        if tag == 0x20:
            return self._parse_collection(offset, depth + 1)
        # dict
        if tag == 0x21:
            return self._parse_object(offset, depth + 1)
        raise PSBError('unknown PSB value tag 0x%02X at 0x%X' % (tag, offset))

    def _parse_collection(self, offset: int, depth: int = 0) -> tuple[list[Any], int]:
        if depth > 400:
            raise PSBError('PSB nesting too deep (corrupt file?)')
        offsets, base = self._read_array(offset + 1)
        values: list[Any] = []
        end = base
        parse = self._parse_value
        for rel in offsets:
            value, value_end = parse(base + rel, depth)
            values.append(value)
            if value_end > end:
                end = value_end
        return values, end

    def _parse_object(self, offset: int, depth: int = 0) -> tuple[dict[str, Any], int]:
        if depth > 400:
            raise PSBError('PSB nesting too deep (corrupt file?)')
        names, names_end = self._read_array(offset + 1)
        offsets, base = self._read_array(names_end)
        obj: dict[str, Any] = {}
        end = base
        get_name = self.get_name
        parse = self._parse_value
        for name_index, rel in zip(names, offsets):
            key = get_name(name_index)
            value, value_end = parse(base + rel, depth)
            obj[key] = value
            if value_end > end:
                end = value_end
        return obj, end

    def read_object_tree(self) -> Any:
        value, _ = self.parse_value(self.header.offset_entries)
        return value

    # ---------------------------------------------------------- resources
    def read_resource_table(self) -> list[dict[str, int]]:
        """回傳 resources 的 (offset, length) 摘要（目前只給統計/除錯用）。"""
        offs = self.read_psb_array(self.header.offset_chunk_offsets)
        lens = self.read_psb_array(self.header.offset_chunk_lengths)
        out = []
        for i in range(min(offs.count, lens.count)):
            out.append({'index': i, 'offset': offs.values[i],
                        'length': lens.values[i]})
        return out

    def read_resource_bytes(self, index: int) -> bytes:
        offs = self.read_psb_array(self.header.offset_chunk_offsets)
        lens = self.read_psb_array(self.header.offset_chunk_lengths)
        if not (0 <= index < offs.count and index < lens.count):
            raise PSBError('resource index %d out of range' % index)
        start = self.header.offset_chunk_data + offs.values[index]
        return self._data[start:start + lens.values[index]]


# ---------------------------------------------------------------------------
# 樹走訪
# ---------------------------------------------------------------------------
def _join(path: str, key: Any) -> str:
    if isinstance(key, int):
        return '%s[%d]' % (path, key)
    return path + '/' + str(key)


def iter_strings(tree: Any, path: str = '') -> Iterator[tuple[str, str, str | None]]:
    """走訪樹的每個字串葉子，回傳 ``(json_path, value, parent_key)``。

    ``parent_key`` 是「值所屬的欄位名」：dict 的值就是 key；list 元素繼承該
    list 所在的欄位名；根節點為 None。迭代順序穩定（depth-first）。
    """
    if isinstance(tree, dict):
        for key, value in tree.items():
            if isinstance(value, str):
                yield _join(path, key), value, key if isinstance(key, str) else None
            else:
                yield from _iter_descend(value, _join(path, key),
                                         key if isinstance(key, str) else None)
    elif isinstance(tree, list):
        for index, value in enumerate(tree):
            yield from _iter_descend(value, _join(path, index), None)


def _iter_descend(value: Any, path: str, parent_key: str | None
                  ) -> Iterator[tuple[str, str, str | None]]:
    if isinstance(value, str):
        yield path, value, parent_key
    elif isinstance(value, dict):
        for key, child in value.items():
            child_path = _join(path, key)
            if isinstance(child, str):
                yield child_path, child, key if isinstance(key, str) else None
            else:
                yield from _iter_descend(child, child_path,
                                         key if isinstance(key, str) else None)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_descend(child, _join(path, index), parent_key)


def iter_all_strings(tree: Any, path: str = ''
                     ) -> Iterator[tuple[str, str, str | None, str]]:
    """像 :func:`iter_strings`，但同時把 dict 的 key 當字串產出。

    回傳 ``(json_path, value, parent_key, role)``，``role`` 是 ``'value'`` 或
    ``'key'``。有些資料結構把檔名放在 key（例如合成 PSB 的
    ``{"10.tlg": {...}}``），所以 facts 抽取需要 key。

    注意：這是「先建好完整 path 字串」的直覺版本，正式抽 Fact 走
    :func:`_walk_lazy`（path 只在真的要產生 Fact 時才組字串，快很多）。
    """
    if isinstance(tree, dict):
        for key, value in tree.items():
            if isinstance(key, str):
                yield path + '/<key>', key, None, 'key'
            child_path = _join(path, key)
            yield from _iter_all_descend(value, child_path,
                                         key if isinstance(key, str) else None)
    elif isinstance(tree, list):
        for index, value in enumerate(tree):
            yield from _iter_all_descend(value, _join(path, index), None)


def _iter_all_descend(value: Any, path: str, parent_key: str | None
                      ) -> Iterator[tuple[str, str, str | None, str]]:
    if isinstance(value, str):
        yield path, value, parent_key, 'value'
    elif isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str):
                yield path + '/<key>', key, None, 'key'
            child_path = _join(path, key)
            yield from _iter_all_descend(child, child_path,
                                         key if isinstance(key, str) else None)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_all_descend(child, _join(path, index), parent_key)


def _walk_lazy(tree: Any, on_value, on_key,
               on_command=None, on_voice=None) -> None:
    """走完整棵樹的所有字串葉子／key，但不在每個節點組 path 字串。

    * ``on_value(parts, value, parent_key, sem_cls, sem_keys)``：字串葉子。
      ``sem_cls`` 非 None 表示這是場景 PSB ``data`` item 的 props
      （class = sem_cls，例如 stage/bgm），``sem_keys`` 是往下的欄位路徑。
    * ``on_key(parts, key)``：dict 的 key。
    * ``on_command(parts, cmd)``：場景 ``lines`` 裡的指令列
      （``[tag, param, value, ...]``）。
    * ``on_voice(parts, voice)``：含 ``voice`` 欄位的 dict（texts 語音清單）。

    效能關鍵：``parts`` 只是 token list，只有真的要產生 Fact 時才 join。
    """
    parts: list[str] = []

    def walk(node: Any, parent_key: str | None, sem_cls: str | None,
             sem_keys: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            if on_voice is not None and 'voice' in node:
                voice = node.get('voice')
                if isinstance(voice, str) and voice:
                    on_voice(parts, voice)
            for key, value in node.items():
                if not isinstance(key, str):
                    continue
                token = '/' + key
                parts.append(token)
                child_keys = sem_keys + (key,) if sem_cls is not None else ()
                if isinstance(value, str):
                    on_value(parts, value, key, sem_cls, child_keys)
                elif isinstance(value, (dict, list)):
                    walk(value, key, sem_cls, child_keys)
                parts.pop()
                on_key(parts, key)
        elif isinstance(node, list):
            if (on_command is not None and parent_key == 'lines' and node
                    and isinstance(node[0], str)):
                on_command(parts, node)
            is_data = (parent_key == 'data')
            for index, value in enumerate(node):
                token = '[%d]' % index
                parts.append(token)
                if (is_data and isinstance(value, list) and len(value) >= 3
                        and isinstance(value[0], str)
                        and isinstance(value[2], dict)):
                    # 場景 PSB 的 data item: [class, instance_name, props]
                    cls = value[0]
                    parts.append('/props')
                    walk(value[2], None, cls, ())
                    parts.pop()
                    # 其他元素（instance name / 少見的額外欄位）走通用
                    for extra in (1,) + tuple(range(3, len(value))):
                        parts.append('[%d]' % extra)
                        child = value[extra]
                        if isinstance(child, str):
                            on_value(parts, child, None, None, ())
                        elif isinstance(child, (dict, list)):
                            walk(child, None, None, ())
                        parts.pop()
                elif isinstance(value, str):
                    on_value(parts, value, parent_key, sem_cls, sem_keys)
                elif isinstance(value, (dict, list)):
                    walk(value, parent_key, sem_cls, sem_keys)
                parts.pop()

    walk(tree, None, None, ())




# ---------------------------------------------------------------------------
# raw fallback：解析失敗時直接掃 bytes 裡的 UTF-8 / UTF-16LE 字串
# ---------------------------------------------------------------------------
_UTF8_RUN = re.compile(rb'[\x20-\x7e\xc2-\xf4][\x20-\x7e\x80-\xbf]*')
_UTF16_RUN = re.compile(rb'(?:[\x20-\x7e]\x00|[\x00-\xff][\x30-\x9f]){4,}')


def iter_raw_strings(data: bytes) -> Iterator[str]:
    seen: set[str] = set()
    for match in _UTF8_RUN.finditer(data):
        raw = match.group()
        if len(raw) < 3:
            continue
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            continue
        if text not in seen:
            seen.add(text)
            yield text
    for match in _UTF16_RUN.finditer(data):
        raw = match.group()
        try:
            text = raw.decode('utf-16le')
        except UnicodeDecodeError:
            continue
        if len(text) < 3:
            continue
        if text not in seen:
            seen.add(text)
            yield text


# ---------------------------------------------------------------------------
# Fact 抽取
# ---------------------------------------------------------------------------
SCENARIO_KEYS = ('scenes', 'commands', 'tags')


def _source(entry: Any, path: str) -> str:
    archive = getattr(entry, 'archive', None) or '?'
    index = getattr(entry, 'index', None)
    if index is None:
        index = getattr(entry, 'idx', '?')
    return '%s#%s:%s' % (archive, index, path)


# 語音 id（實際檔名 = <id>.ogg / <id>.ogg.sli）
VOICE_ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9]{1,15}_\d{3}_\d{4}[A-Za-z]?$')
# 「像 storage 的短字串」：無空白、無程式碼/標點符號、<=64。
_LEAF_BAD_CHARS = frozenset(
    ' \t\r\n\x00、。，．,;:!?！？…‥・「」『』（）()[]{}<>"\'`'
    '%&*+=~^|\\@#＃＄$')


def looks_like_storage_leaf(value: str) -> bool:
    """沒有副檔名、但像「storage/場景名/角色零件名」的短字串（generic fuse）。"""
    if not value or len(value) > 64:
        return False
    if any(ch in _LEAF_BAD_CHARS for ch in value):
        return False
    # 至少要有字母/數字/底線/斜線或非 ASCII 字元
    for ch in value:
        if ch.isalnum() or ch in '_/':
            return True
        if ord(ch) > 127:
            return True
    return False


def split_voice_value(value: str) -> str | None:
    """語音欄位可能是 ``<voice_id>|<DSP/效果名>`` 的複合值（實測 1245 筆）。

    回傳複合值裡的 voice id（可 hash 的那段），不是複合值就回 None。
    """
    if '|' not in value:
        return None
    prefix = value.split('|', 1)[0]
    if VOICE_ID_RE.match(prefix):
        return prefix
    return None


def _classify(value: str, parent_key: str | None
              ) -> tuple[str, str, str] | None:
    """回傳 ``(kind, context, note)``；不該成為 Fact 時回 None。"""
    if not value:
        return None
    # 目錄 entry
    if looks_like_dir(value):
        context = 'psb:dir'
        if parent_key in DIR_KEYS:
            context = 'psb:' + parent_key
        return KIND_DIR, context, 'directory-like string in PSB'
    # 完整檔名
    if looks_like_filename(value):
        context = 'psb:value'
        if parent_key:
            context = 'psb:' + parent_key
        return KIND_NAME, context, 'string ending with a known file extension'
    # 純 hash：不是檔名，跳過
    if is_hex_hash(value):
        return None
    # 複合語音值（<voice_id>|<DSP…>）：只取可 hash 的前段
    voice_id = split_voice_value(value)
    if voice_id is not None:
        return (KIND_FRAGMENT, 'psb:voice.storage',
                'voice id split from compound value %r' % value)
    # 語音 id（任何欄位裡都可能出現；generic fuse）
    if VOICE_ID_RE.match(value):
        return (KIND_FRAGMENT, 'psb:voice',
                'voice id string (entry name = this + ".ogg")')
    # 檔名語意欄位
    if parent_key in NAMEISH_KEYS:
        return (KIND_FRAGMENT, 'psb:' + parent_key,
                'value of filename-semantic field %r' % parent_key)
    # generic fuse：任何 key 下的「像 storage」短字串
    if looks_like_storage_leaf(value):
        return (KIND_FRAGMENT, 'psb:leaf',
                'short storage-like string under arbitrary key %r' % parent_key)
    return None



# ---------------------------------------------------------------------------
# 場景 PSB（編譯後 KAG）語意抽取
# ---------------------------------------------------------------------------
# 編譯後場景的結構（實測 tenshi 191 個 .scn PSB）：
#
#   root { name, llmap, scenes[] }
#   scene { label, title, lines[], texts[], nexts[], ... }
#   lines[i]:
#     * 指令列（KAG tag 編譯後）：[tag, param, value, param, value, ...]
#       e.g. ["playvoice","loop",1,"name","天音","voice","ama_002_0057.ogg"]
#            ["sysmovie","file","OP","flag","movie_op","bgmflag","SongOP"]
#            ["phonechat","add","true","stamp","stamp_EV201AA","photo","EV201AA"]
#     * 狀態更新列：以數字開頭，含 {"data": [[class, instance, props], ...]}
#   data item: [class, instance_name, props]
#       stage.redraw.imageFile.file = '空_昼'        → 空_昼.png（實測 hash 命中）
#       bgm.replay.filename         = 'bgm01'       → bgm01.opus / .opus.sli
#       character.redraw.imageFile.file = '天音.stand' → 已是完整檔名
#       lse.replay.filename         = '■浜辺'       → ■浜辺.ogg
#   texts[j][2] = [{"voice": "ama_002_0057", ...}]   → 語音 stem（+.ogg/.ogg.sli）
#
# 這裡只負責「把值與語意位置抽出來」；副檔名規則在 rules.py。
_DATA_STRUCTURAL_KEYS = frozenset({'redraw', 'replay', 'update', 'action',
                                   'uistates', 'metadata', 'link', 'posName'})
_FILE_LEAF_KEYS = frozenset({'file', 'filename', 'storage', 'voice', 'stamp',
                             'photo', 'src', 'script', 'font', 'movie',
                             'image', 'bgm', 'se', 'path', 'refile', 'sename',
                             'icon', 'texture', 'model', 'motion', 'pack'})
# 指令列裡「值可能就是檔名」的參數名
_COMMAND_FILE_PARAMS = frozenset({'file', 'filename', 'storage', 'voice',
                                  'stamp', 'photo', 'src', 'script', 'font',
                                  'movie', 'image', 'bgm', 'se', 'icon',
                                  'path', 'refile', 'sename'})


def _dotted(keys: list[str]) -> str:
    kept = [k for k in keys if k not in _DATA_STRUCTURAL_KEYS]
    return '.'.join(kept[-3:]) if kept else ''


def _semantic_context(cls: str, keys: tuple[str, ...]) -> str:
    dotted = _dotted(list(keys))
    return ('psb:%s.%s' % (cls, dotted)) if dotted else ('psb:' + cls)


def iter_facts(entry: Any, data: bytes) -> Iterator[Fact]:
    """把一個 PSB entry 的所有檔名候選變成 :class:`Fact` 串流。

    ``entry`` 需有 ``.archive`` / ``.index``（corpus.CorpusEntry 介面）。
    解析失敗時退回 raw 字串掃描，並把失敗原因塞進 note（不會丟例外）。

    流程（單次走訪；語意優先、generic fuse 補漏）：

    1. 場景 PSB 的 ``data`` items（class=stage/bgm/lse/…）：欄位語意 context
       ``psb:<class>.<field>``。
    2. 場景 PSB 的指令列（``[tag, param, value, ...]``）：``psb:<tag>.<param>``。
    3. texts 的 ``voice`` 欄位（含 ``<id>|<DSP>`` 複合值切分）：``psb:voice.storage``。
    4. nexts 的 ``storage``（下個場景 .ks 名）：``psb:nexts.storage``。
    5. 通用：所有字串葉子
       * 有已知副檔名 → NAME
       * 語音 id（regex）→ FRAGMENT ``psb:voice``
       * 像 storage 的短字串 → FRAGMENT ``psb:leaf``
    6. dict key 像檔名 → NAME ``psb:key``。
    """
    parsed = True
    tree: Any = None
    error = ''
    try:
        tree = PSBReader(data).read_object_tree()
    except Exception as ex:                     # noqa: BLE001 - 壞檔不能炸 pipeline
        parsed = False
        error = '%s: %s' % (type(ex).__name__, ex)
    # 去重（(kind, value)）：同一個值可能先被 generic 看到、後被語意規則看到，
    # 這裡保留「語意優先權較高」的那筆 context（semantic > generic）。
    out: list[Fact] = []
    index: dict[tuple[str, str], int] = {}
    priority: list[int] = []

    def emit(value: str, kind: str, path: str, context: str, note: str,
             prio: int = 0) -> None:
        key = (kind, value)
        pos = index.get(key)
        if pos is not None:
            if prio > priority[pos]:
                out[pos] = Fact(value=value, kind=kind,
                                source=_source(entry, path),
                                context=context, note=note)
                priority[pos] = prio
            return
        index[key] = len(out)
        out.append(Fact(value=value, kind=kind, source=_source(entry, path),
                        context=context, note=note))
        priority.append(prio)

    if parsed:
        scenario = (isinstance(tree, dict)
                    and isinstance(tree.get('scenes'), list)
                    and any(k in tree for k in SCENARIO_KEYS))

        def on_value(parts: list[str], value: str, parent_key: str | None,
                     sem_cls: str | None, sem_keys: tuple[str, ...]) -> None:
            if not value:
                return
            # 1) scenario data item 的檔名語意欄位（class = stage/bgm/…）
            if sem_cls is not None and parent_key in _FILE_LEAF_KEYS:
                context = _semantic_context(sem_cls, sem_keys)
                if looks_like_filename(value):
                    emit(value, KIND_NAME, ''.join(parts), context,
                         'file reference inside scenario data item (class=%r)'
                         % sem_cls, 3)
                    return
                if not is_hex_hash(value):
                    emit(value, KIND_FRAGMENT, ''.join(parts), context,
                         'stem of a file reference inside scenario data item '
                         '(class=%r, field=%r)' % (sem_cls, parent_key), 3)
                    return
            # 2) nexts[].storage：下一個場景 .ks 名（entry = + ".scn"）
            if (parent_key == 'storage' and len(parts) >= 2
                    and parts[-2] == '/nexts'):
                emit(value, KIND_FRAGMENT, ''.join(parts), 'psb:nexts.storage',
                     'next-scene storage name; entry name = this + ".scn"', 3)
                return
            # 3) PBD data pack 名（`<base>__pack` → entry `<base>.pbd`）
            if value.endswith('__pack') and len(value) > 6:
                base = value[:-6]
                emit(base + '.pbd', KIND_FRAGMENT, ''.join(parts),
                     'psb:packbase',
                     'PBD data pack name %r; entry name = %s.pbd'
                     % (value, base), 3)
                return
            # 3) 通用分類（含語音 id regex / storage-like / NAME）
            info = _classify(value, parent_key)
            if info is None:
                return
            kind, context, note = info
            emit(value, kind, ''.join(parts), context, note,
                 1 if kind in (KIND_NAME, KIND_DIR) else 0)

        def on_key(parts: list[str], key: str) -> None:
            if '.' in key and looks_like_filename(key) and not is_hex_hash(key):
                emit(key, KIND_NAME, ''.join(parts) + '/<key>', 'psb:key',
                     'dict key of PSB object that looks like a filename', 1)

        def on_command(parts: list[str], cmd: list) -> None:
            tag = cmd[0]
            path = ''.join(parts)
            index = 1
            while index + 1 < len(cmd):
                key, value = cmd[index], cmd[index + 1]
                if (isinstance(key, str) and isinstance(value, str)
                        and key in _COMMAND_FILE_PARAMS and value
                        and value not in ('true', 'false', 'null')):
                    context = 'psb:%s.%s' % (tag, key)
                    voice_id = split_voice_value(value)
                    if voice_id is not None:
                        emit(voice_id, KIND_FRAGMENT,
                             '%s[%d]' % (path, index + 1), context,
                             'voice id split from compound value %r' % value, 3)
                    elif looks_like_filename(value):
                        emit(value, KIND_NAME, '%s[%d]' % (path, index + 1),
                             context, 'file reference in command %r' % tag, 3)
                    elif VOICE_ID_RE.match(value):
                        emit(value, KIND_FRAGMENT, '%s[%d]' % (path, index + 1),
                             context, 'voice id in command %r' % tag, 3)
                    elif len(value) <= 255 and not value.endswith('__pack'):
                        emit(value, KIND_FRAGMENT,
                             '%s[%d]' % (path, index + 1), context,
                             'stem of a file reference in command %r' % tag, 3)
                    index += 2
                else:
                    index += 1

        def on_voice(parts: list[str], voice: str) -> None:
            candidate = split_voice_value(voice) or voice
            if candidate:
                emit(candidate, KIND_FRAGMENT, ''.join(parts) + '/voice',
                     'psb:voice.storage',
                     'voice storage stem from scenario text; entry name = '
                     'this + ".ogg" (+ ".ogg.sli")', 3)

        _walk_lazy(tree, on_value, on_key, on_command, on_voice)
        for fact in out:
            yield fact
        out.clear()

        if scenario:
            # 場景 PSB 的 root name 是編譯後 KAG 場景名（entry = + ".scn"）
            scn_name = tree.get('name')
            if isinstance(scn_name, str) and scn_name:
                emit(scn_name, KIND_FRAGMENT, '/name', 'psb:scn_name',
                     'root name of compiled KAG scenario; entry name = this '
                     '+ ".scn"', 3)
                for fact in out:
                    yield fact
                out.clear()

    if not parsed:
        # fallback：raw 字串掃描，任何像檔名/目錄的字串都收，附上解析失敗原因
        for value in iter_raw_strings(data):
            if looks_like_dir(value):
                emit(value, KIND_DIR, '<raw>', 'psb:raw:dir',
                     'raw scan fallback (%s)' % error)
            elif looks_like_filename(value) and not is_hex_hash(value):
                emit(value, KIND_NAME, '<raw>', 'psb:raw',
                     'raw scan fallback (%s)' % error)
        for fact in out:
            yield fact
        out.clear()


def iter_facts_from_path(path: str | Path, *,
                         archive: str | None = None,
                         index: int | None = None
                         ) -> Iterator[Fact]:
    """獨立測試用：直接吃檔案路徑。"""
    path = Path(path)
    if archive is None:
        archive = path.parent.name
    if index is None:
        match = re.search(r'(\d+)', path.stem)
        index = int(match.group(1)) if match else 0

    class _E:
        pass

    entry = _E()
    entry.archive = archive
    entry.index = index
    entry.hash = ''
    entry.path = str(path)
    yield from iter_facts(entry, path.read_bytes())


# 舊介面相容：讓其他人可以用 PSBFile/PSBReader 讀樹
PSBFile = PSBReader


__all__ = [
    'KINDS', 'PSBError', 'PSBMagicError', 'PSBVersionError', 'PSBHeader',
    'PSBArray', 'PSBReader', 'PSBFile', 'parse_header',
    'iter_strings', 'iter_all_strings', 'iter_raw_strings',
    'iter_facts', 'iter_facts_from_path', 'looks_like_filename',
    'looks_like_dir', 'is_hex_hash', 'KNOWN_EXTS', 'NAMEISH_KEYS',
]
