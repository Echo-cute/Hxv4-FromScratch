"""pbd.py -- PBD（TJS/4s0 加密容器，或已是 TJS/ns0 的變體樹）解析 + Fact 抽取。

PBD 是什麼
==========

KiriKiri／M2 用來描述「圖層清單」的容器：

* ``TJS/4s0\\0``：加密版。header 之後可選 IV，再經
  「BLAKE2s(keyed MAC) 衍鑰 + XXH32 nonce + ChaCha8/xorshift keystream」XOR，
  最後是 LZ4 block 串流（每塊 u16 長度前綴，64KiB dictionary）。
  解密後是本模組要解析的 TJS variant 樹。
* ``TJS/ns0\\0``：同一種 variant 樹的明文版（引擎執行期／部分 patch 直接放這種）。

這個實作是**純 Python**（不呼叫 pbd2json.exe）：
``tjs_ns0/tjs_ns0.rs``（msg-tool）描述的格式，本檔重新實作並以
``lib/pbd2json.exe`` 對 44 個 fgimage/upgrade 的 4s0 檔驗證輸出一致。

variant 樹編碼（小端）：

* u16 tag = ``(check_byte << 8) | type_byte``；check byte 由 seed 驅動的
  LCG-ish checker 產生（ns0 一種、4s0 一種，兩者都實作在下面）。
* type 0 = null、2 = UTF-16LE 字串（u32 字元數）、4 = int64、
  5 = double、0x81 = array（u32 count）、0xC1 = dict（u32 count，key 為字串）。
* 樹尾有 4-byte checksum（忽略）。

Fact 抽取
=========

* 樹是 list（fgimage 立繪零件清單）→ 每筆 layer 的 ``name`` 是檔名片段
  （``<base>_<layer_name>_<layer_id>.tlg`` 的 middle 段）、layer_id 給
  rules.py 組合用 → 寫進 ``records``（給 Lead），並把 name 丟 FRAGMENT。
* 樹是 dict（UI 零件清單）→ 走所有字串：有副檔名的 → NAME；
  ``storage``/``name``/``uiname``/``filename`` 等欄位 → FRAGMENT。
* 不做 hash、不自行組合；組合規則集中 rules.py。
"""
from __future__ import annotations

import hashlib
import struct
import sys
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent
LIB = HERE.parent.parent
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from hxlib.facts import Fact, KIND_NAME, KIND_DIR, KIND_FRAGMENT  # noqa: E402

KINDS = ('pbd', 'tjs_ns')

MASK32 = 0xFFFFFFFF
MAGIC_4S0 = b'TJS/4s0'
MAGIC_NS0 = b'TJS/ns0'

# 檔名語意欄位（UI 零件清單裡的 storage 就是圖檔 base 名）
NAMEISH_KEYS = frozenset({
    'storage', 'file', 'filename', 'filepath', 'path', 'name', 'uiname',
    'base', 'basename', 'resname', 'resource', 'packbase', 'pack',
    'variant', 'motion', 'target', 'src', 'image', 'icon', 'face',
})
KNOWN_EXTS = (
    '.tlg', '.png', '.ogg', '.opus', '.psb', '.scn', '.pbd', '.tjs', '.ks',
    '.mtn', '.asd', '.tft', '.otf', '.ttf', '.mp4', '.wmv', '.sli', '.toml',
    '.ini', '.csv', '.txt', '.stage', '.stand', '.sinfo', '.jpg', '.jpeg',
    '.wav', '.pimg', '.func',
)
_EXT_SUFFIXES = tuple(KNOWN_EXTS)


class PBDError(Exception):
    """PBD/TJS variant 解析錯誤。"""


# ---------------------------------------------------------------------------
# 基本加密原語（純 Python，速度對 167 個小檔足夠）
# ---------------------------------------------------------------------------
_P1 = 0x9E3779B1
_P2 = 0x85EBCA77
_P3 = 0xC2B2AE3D
_P4 = 0x27D4EB2F
_P5 = 0x165667B1


def _rotl(x: int, r: int) -> int:
    return ((x << r) | (x >> (32 - r))) & MASK32


def xxh32(data: bytes, seed: int = 0) -> int:
    """XXH32（msg-tool 用它算 nonce）；與 xxhash_rust::xxh32 對齊。"""
    n = len(data)
    i = 0
    if n >= 16:
        v1 = (seed + _P1 + _P2) & MASK32
        v2 = (seed + _P2) & MASK32
        v3 = seed & MASK32
        v4 = (seed - _P1) & MASK32

        def rnd(v: int, lane: int) -> int:
            v = (v + lane * _P2) & MASK32
            v = _rotl(v, 13)
            return (v * _P1) & MASK32

        while i + 16 <= n:
            v1 = rnd(v1, struct.unpack_from('<I', data, i)[0])
            v2 = rnd(v2, struct.unpack_from('<I', data, i + 4)[0])
            v3 = rnd(v3, struct.unpack_from('<I', data, i + 8)[0])
            v4 = rnd(v4, struct.unpack_from('<I', data, i + 12)[0])
            i += 16
        h = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & MASK32
    else:
        h = (seed + _P5) & MASK32
    h = (h + n) & MASK32
    while i + 4 <= n:
        h = (h + struct.unpack_from('<I', data, i)[0] * _P3) & MASK32
        h = (_rotl(h, 17) * _P4) & MASK32
        i += 4
    while i < n:
        h = (h + data[i] * _P5) & MASK32
        h = (_rotl(h, 11) * _P1) & MASK32
        i += 1
    h ^= h >> 15
    h = (h * _P2) & MASK32
    h ^= h >> 13
    h = (h * _P3) & MASK32
    h ^= h >> 16
    return h


def _quarter_round(s: list[int], a: int, b: int, c: int, d: int) -> None:
    s[a] = (s[a] + s[b]) & MASK32
    s[d] = _rotl(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & MASK32
    s[b] = _rotl(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & MASK32
    s[d] = _rotl(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & MASK32
    s[b] = _rotl(s[b] ^ s[c], 7)


def chacha_block(key: bytes, nonce_lo: int, nonce_hi: int, counter: int,
                 rounds: int) -> bytes:
    state = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574]
    state += list(struct.unpack('<8I', key))
    state += [counter & MASK32, (counter >> 32) & MASK32,
              nonce_lo & MASK32, nonce_hi & MASK32]
    work = list(state)
    r = rounds
    while r > 0:
        _quarter_round(work, 0, 4, 8, 12)
        _quarter_round(work, 1, 5, 9, 13)
        _quarter_round(work, 2, 6, 10, 14)
        _quarter_round(work, 3, 7, 11, 15)
        _quarter_round(work, 0, 5, 10, 15)
        _quarter_round(work, 1, 6, 11, 12)
        _quarter_round(work, 2, 7, 8, 13)
        _quarter_round(work, 3, 4, 9, 14)
        r -= 2
    return struct.pack('<16I', *[((work[i] + state[i]) & MASK32)
                                 for i in range(16)])


def _xorshift32(x: int, fallback: int) -> int:
    x &= MASK32
    x ^= (x << 13) & MASK32
    x ^= x >> 17
    x ^= (x << 5) & MASK32
    return x if x else fallback


# header.crypt field -> (ChaCha rounds, keystream expansion factor)
_CRYPT_PARAMS = {1: (8, 0x10), 2: (0xC, 8), 3: (0x14, 4),
                 4: (8, 1), 5: (0xC, 1), 6: (0x14, 1)}


def _gen_keystream(key: bytes, nonce_lo: int, nonce_hi: int, counter: int,
                   p4: int, rounds: int, fallback: int) -> bytes:
    block = chacha_block(key, nonce_lo, nonce_hi, counter, rounds)
    if p4 <= 1:
        return block
    words = list(struct.unpack('<16I', block))
    for j in range((p4 * 4 - 4) * 4):
        words.append(_xorshift32(words[j], fallback))
    return struct.pack('<%dI' % len(words), *words)


def stream_crypt_decrypt(body: bytes, seed: int, iv: bytes, field: int) -> bytes:
    """TJS/4s0 的 XOR stream 解密。"""
    rounds, p4 = _CRYPT_PARAMS[field]
    # 衍鑰：BLAKE2s keyed MAC（key = seed LE bytes，message = IV）
    key = hashlib.blake2s(b'', digest_size=32,
                          key=seed.to_bytes(4, 'little')).digest()
    nonce_lo = xxh32(iv, seed)
    nonce_hi = seed
    fallback = nonce_hi ^ nonce_lo
    if fallback == 0:
        fallback = seed if seed else MASK32
    out = bytearray(len(body))
    produced = 0
    counter = 0
    while produced < len(body):
        ks = _gen_keystream(key, nonce_lo, nonce_hi, counter, p4, rounds, fallback)
        n = min(len(ks), len(body) - produced)
        for i in range(n):
            out[produced + i] = body[produced + i] ^ ks[i]
        produced += n
        counter += 1
    return bytes(out)


# ---------------------------------------------------------------------------
# LZ4 block / stream（含 64KiB dictionary）
# ---------------------------------------------------------------------------
def lz4_block_decompress(src: bytes, dictionary: bytes = b'') -> bytes:
    """LZ4 block format 解壓；match 允許指回 ``dictionary``（前面的輸出）。"""
    out = bytearray()
    pos = 0
    size = len(src)
    while pos < size:
        token = src[pos]
        pos += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                if pos >= size:
                    raise PBDError('lz4 literal length truncated')
                b = src[pos]
                pos += 1
                lit_len += b
                if b != 255:
                    break
        if pos + lit_len > size:
            raise PBDError('lz4 literals exceed block')
        out += src[pos:pos + lit_len]
        pos += lit_len
        if pos >= size:
            break
        if pos + 2 > size:
            raise PBDError('lz4 offset truncated')
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0:
            raise PBDError('lz4 zero offset')
        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                if pos >= size:
                    raise PBDError('lz4 match length truncated')
                b = src[pos]
                pos += 1
                match_len += b
                if b != 255:
                    break
        start = len(out) - offset
        if start < -len(dictionary):
            raise PBDError('lz4 match before dictionary')
        for k in range(match_len):
            idx = start + k
            if idx < 0:
                out.append(dictionary[len(dictionary) + idx])
            else:
                out.append(out[idx])
    return bytes(out)


def lz4_stream_decompress(data: bytes) -> bytes:
    """TJS/4s0 的 LZ4 串流：``[u16 csize][LZ4 block]*``，dict = 前 64KiB。"""
    out = bytearray()
    pos = 0
    while pos + 2 <= len(data):
        csize = struct.unpack_from('<H', data, pos)[0]
        if csize == 0:
            break
        if pos + 2 + csize > len(data):
            break
        chunk = data[pos + 2:pos + 2 + csize]
        dictionary = bytes(out[-65536:]) if out else b''
        try:
            block = lz4_block_decompress(chunk, dictionary)
        except PBDError:
            block = lz4_block_decompress(chunk, b'')
        out += block
        pos += 2 + csize
    if not out and data:
        last_error: Exception = PBDError('empty lz4 output')
        for skip in (0, 2):
            try:
                return lz4_block_decompress(data[skip:])
            except Exception as ex:      # noqa: BLE001
                last_error = ex
        raise PBDError('lz4 stream decompress failed: %s' % last_error)
    return bytes(out)


# ---------------------------------------------------------------------------
# variant 樹
# ---------------------------------------------------------------------------
class _ByteChecker:
    """TJS/ns0 的 check-byte 產生器（上游 ByteChecker）。"""

    __slots__ = ('seed',)

    def __init__(self, seed: int):
        self.seed = seed

    @staticmethod
    def _round(seed: bytearray) -> None:
        a = (seed[0] ^ ((seed[0] << 1) & 0xFF)) & 0xFF
        b = a
        b = (b >> 2) & 0xFF
        b = (b ^ seed[2]) & 0xFF
        b = (b >> 3) & 0xFF
        b = (b ^ seed[2]) & 0xFF
        b = (b ^ a) & 0xFF
        seed[0] = seed[1]
        seed[1] = seed[2]
        seed[2] = b

    def get_seed(self, type_code: int) -> int:
        s = bytearray(self.seed.to_bytes(4, 'little'))
        if type_code == 0:
            return s[2]
        self._round(s)
        self.seed = int.from_bytes(s, 'little')
        return s[2]


class _PackinOneChecker:
    """TJS/4s0（PackinOne 引擎）的 check-byte 產生器。"""

    __slots__ = ('b0', 'b1', 'b2')

    def __init__(self, seed: int):
        self.b0 = ((seed >> 24) & 0xFF) ^ (seed & 0xFF)
        self.b1 = (seed >> 8) & 0xFF
        self.b2 = (seed >> 16) & 0xFF

    def _step(self) -> int:
        t = (self.b0 ^ ((self.b0 << 1) & 0xFF)) & 0xFF
        self.b0 = self.b1
        self.b1 = self.b2
        self.b2 = (self.b2 ^ (self.b2 >> 3) ^ t ^ (t >> 5)) & 0xFF
        return self.b2

    def get_seed(self, type_code: int) -> int:
        return self.b2 if type_code == 0 else self._step()


class VariantReader:
    """解析 TJS/ns0|4s0 的 variant 串流（小端、UTF-16LE 字串）。"""

    def __init__(self, data: bytes, checker: Any):
        self.data = data
        self.pos = 0
        self.checker = checker

    def _u16(self) -> int:
        value = struct.unpack_from('<H', self.data, self.pos)[0]
        self.pos += 2
        return value

    def _u32(self) -> int:
        value = struct.unpack_from('<I', self.data, self.pos)[0]
        self.pos += 4
        return value

    def _string(self) -> str:
        count = self._u32()
        end = self.pos + count * 2
        if count > 0x4000000 or end > len(self.data):
            raise PBDError('string length %d out of range at 0x%X'
                           % (count, self.pos - 4))
        text = self.data[self.pos:end].decode('utf-16le', errors='replace')
        self.pos = end
        return text

    def value(self, depth: int = 0) -> Any:
        if depth > 400:
            raise PBDError('variant nesting too deep')
        if self.pos + 2 > len(self.data):
            raise PBDError('unexpected end of variant stream')
        typ = self._u16()
        type_byte = typ & 0xFF
        check_byte = typ >> 8
        expected = self.checker.get_seed(type_byte)
        if check_byte != expected:
            raise PBDError(
                'TJS check byte mismatch at 0x%X: tag 0x%02X expected 0x%02X '
                'got 0x%02X' % (self.pos - 2, type_byte, expected, check_byte))
        if type_byte == 0:
            return None
        if type_byte == 2:
            return self._string()
        if type_byte == 4:
            value = struct.unpack_from('<q', self.data, self.pos)[0]
            self.pos += 8
            return value
        if type_byte == 5:
            value = struct.unpack_from('<d', self.data, self.pos)[0]
            self.pos += 8
            return value
        if type_byte == 0x81:
            count = self._u32()
            if count > 0x4000000:
                raise PBDError('array count %d out of range' % count)
            return [self.value(depth + 1) for _ in range(count)]
        if type_byte == 0xC1:
            count = self._u32()
            if count > 0x4000000:
                raise PBDError('dict count %d out of range' % count)
            out: dict[str, Any] = {}
            for _ in range(count):
                key = self._string()
                out[key] = self.value(depth + 1)
            return out
        raise PBDError('unsupported variant type 0x%02X at 0x%X'
                       % (type_byte, self.pos - 2))


def parse_pbd(data: bytes) -> tuple[Any, dict[str, Any]]:
    """解析 TJS/4s0 或 TJS/ns0，回傳 ``(variant 樹, 摘要 dict)``。"""
    if len(data) < 16:
        raise PBDError('file too short: %d bytes' % len(data))
    magic = data[:4]
    check = data[4:8]
    if magic != b'TJS/':
        raise PBDError('bad PBD magic %r' % (magic,))
    seed, crypt, iv_len = struct.unpack_from('<IHH', data, 8)
    pos = 16
    if check[0] == ord('4'):
        iv = data[pos:pos + iv_len]
        if len(iv) != iv_len:
            raise PBDError('IV truncated')
        pos += iv_len
        body = data[pos:]
        if crypt:
            if crypt not in _CRYPT_PARAMS:
                raise PBDError('unsupported PBD crypt field %d' % crypt)
            body = stream_crypt_decrypt(body, seed, iv, crypt)
        plain = lz4_stream_decompress(body)
        reader = VariantReader(plain, _PackinOneChecker(seed))
        tree = reader.value()
        info = {'magic': 'TJS/4s0', 'seed': seed, 'crypt': crypt,
                'iv_len': iv_len, 'plain_size': len(plain)}
    elif check[0] == ord('n'):
        if crypt:
            raise PBDError('encrypted TJS/ns0 not supported')
        if iv_len:
            raise PBDError('TJS/ns0 with IV not supported')
        reader = VariantReader(data[pos:], _ByteChecker(seed))
        tree = reader.value()
        info = {'magic': 'TJS/ns0', 'seed': seed, 'crypt': crypt,
                'iv_len': iv_len}
    else:
        raise PBDError('unsupported TJS container %r' % (check,))
    if reader.pos + 4 <= len(reader.data):
        info['trailing_checksum'] = struct.unpack_from(
            '<I', reader.data, reader.pos)[0]
    info['trailing_bytes'] = len(reader.data) - reader.pos
    return tree, info


def parse_pbd_path(path: str | Path) -> tuple[Any, dict[str, Any]]:
    return parse_pbd(Path(path).read_bytes())


# ---------------------------------------------------------------------------
# layer records（給 Lead 的 rules.py 組合檔名用）
# ---------------------------------------------------------------------------
_LAYER_FIELDS = ('name', 'layer_id', 'group_layer_id', 'layer_type', 'type',
                 'left', 'top', 'width', 'height', 'opacity', 'visible')


def layer_records(tree: Any) -> dict[str, Any]:
    """把 variant 樹正規化成給 rules.py 用的記錄。

    * list 樹（立繪零件清單）：第一筆是畫布 ``{width,height}``，其後每筆是 layer
      → ``{'kind': 'layer_list', 'canvas': {...}, 'layers': [{...}, ...]}``
    * dict 樹（UI 零件清單）：``{'kind': 'object', 'root': {...}}``，其中
      ``result``/``names`` 等結構原樣保留。
    """
    if isinstance(tree, list):
        canvas = None
        layers: list[dict[str, Any]] = []
        for item in tree:
            if not isinstance(item, dict):
                continue
            if 'layer_id' in item:
                layers.append({k: item.get(k) for k in _LAYER_FIELDS
                               if k in item})
            elif 'width' in item and 'height' in item and canvas is None:
                canvas = {'width': item.get('width'),
                          'height': item.get('height')}
        return {'kind': 'layer_list', 'canvas': canvas, 'layers': layers,
                'raw_count': len(tree)}
    if isinstance(tree, dict):
        return {'kind': 'object', 'root': tree}
    return {'kind': 'scalar', 'root': tree}


# ---------------------------------------------------------------------------
# Fact 抽取
# ---------------------------------------------------------------------------
def _source(entry: Any, path: str) -> str:
    archive = getattr(entry, 'archive', None) or '?'
    index = getattr(entry, 'index', None)
    if index is None:
        index = getattr(entry, 'idx', '?')
    return '%s#%s:%s' % (archive, index, path)


def _is_filename(value: str) -> bool:
    if not value or len(value) > 255:
        return False
    if '\x00' in value or '\n' in value or '\r' in value:
        return False
    low = value.lower()
    return any(low.endswith(ext) for ext in _EXT_SUFFIXES)


def _is_dir(value: str) -> bool:
    return (len(value) > 1 and value.endswith('/') and '//' not in value
            and not value.startswith('/'))


def _walk_strings(node: Any, path: str = '', parent_key: str | None = None
                  ) -> Iterator[tuple[str, str, str | None]]:
    if isinstance(node, dict):
        for key, value in node.items():
            child = path + '/' + str(key)
            if isinstance(value, str):
                yield child, value, key if isinstance(key, str) else None
            else:
                yield from _walk_strings(value, child,
                                         key if isinstance(key, str) else None)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child = path + '[%d]' % index
            if isinstance(value, str):
                yield child, value, parent_key
            else:
                yield from _walk_strings(value, child, parent_key)


def iter_facts(entry: Any, data: bytes) -> Iterator[Fact]:
    """把一個 PBD entry 的檔名候選變成 :class:`Fact` 串流（不丟例外）。"""
    try:
        tree, _info = parse_pbd(data)
    except Exception:                  # noqa: BLE001 - 壞檔不能炸 pipeline
        return
    seen: set[tuple[str, str]] = set()

    def emit(value: str, kind: str, path: str, context: str, note: str):
        key = (kind, value)
        if key in seen:
            return None
        seen.add(key)
        return Fact(value=value, kind=kind, source=_source(entry, path),
                    context=context, note=note)

    # 1) 樹是 layer list：layer.name 是檔名中段（例如 "0"、"0_1"）。
    if isinstance(tree, list):
        for index, item in enumerate(tree):
            if not isinstance(item, dict):
                continue
            if 'layer_id' in item:
                name = item.get('name')
                if isinstance(name, str) and name:
                    fact = emit(name, KIND_FRAGMENT,
                                '/layers[%d]/name' % index, 'pbd:layer',
                                'PBD layer name (layer_id=%s); entry name is '
                                '<base>_<layer_name>_<layer_id>.tlg'
                                % item.get('layer_id'))
                    if fact:
                        yield fact
                for field in ('filename', 'storage', 'file', 'base'):
                    value = item.get(field)
                    if isinstance(value, str) and value:
                        fact = emit(value, KIND_FRAGMENT,
                                    '/layers[%d]/%s' % (index, field),
                                    'pbd:' + field,
                                    'PBD layer field %r' % field)
                        if fact:
                            yield fact
            else:
                for path, value, parent_key in _walk_strings(item):
                    if _is_filename(value):
                        fact = emit(value, KIND_NAME, path, 'pbd:value',
                                    'PBD canvas/filename string')
                        if fact:
                            yield fact

    # 2) 樹是 object（UI 零件清單）：走所有字串。
    for path, value, parent_key in _walk_strings(tree):
        if _is_dir(value):
            fact = emit(value, KIND_DIR, path, 'pbd:dir',
                        'directory-like string in PBD')
            if fact:
                yield fact
            continue
        # PBD data pack：`<base>__pack` → entry `<base>.pbd`（t-gap 驗證 24/24）
        if value.endswith('__pack') and len(value) > 6:
            base = value[:-6]
            fact = emit(base + '.pbd', KIND_FRAGMENT, path, 'pbd:packbase',
                        'PBD data pack name %r; entry name = %s.pbd'
                        % (value, base))
            if fact:
                yield fact
            continue
        if _is_filename(value):
            context = 'pbd:' + parent_key if parent_key else 'pbd:value'
            fact = emit(value, KIND_NAME, path, context,
                        'PBD string ending with a known file extension')
            if fact:
                yield fact
            continue
        if parent_key in NAMEISH_KEYS and len(value) <= 255:
            fact = emit(value, KIND_FRAGMENT, path, 'pbd:' + parent_key,
                        'value of PBD filename-semantic field %r' % parent_key)
            if fact:
                yield fact


def iter_facts_from_path(path: str | Path, *, archive: str | None = None,
                         index: int | None = None) -> Iterator[Fact]:
    """獨立測試用：直接吃檔案路徑。"""
    path = Path(path)
    if archive is None:
        archive = path.parent.name
    if index is None:
        import re
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


__all__ = [
    'KINDS', 'PBDError', 'xxh32', 'chacha_block', 'stream_crypt_decrypt',
    'lz4_block_decompress', 'lz4_stream_decompress', 'VariantReader',
    'parse_pbd', 'parse_pbd_path', 'layer_records', 'iter_facts',
    'iter_facts_from_path',
]
