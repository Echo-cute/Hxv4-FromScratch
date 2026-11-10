"""tjs.py -- TJS2 script / data container parsing for the Hxv4 name-recovery tool.

Three container families are recognised (magic at offset 0):

===================  ==========================================================
``TJS2100\\0``         TJS2 compiled **bytecode**.  ``DATA`` chunk holds the
                      literal pools (strings / octets / numbers), ``OBJS``
                      chunk holds the code objects.  This is what the vendor
                      decompiler (``vendor/.../tools/tjs2-decompiler``) reads.
``TJS/ns0\\0``         TJS2 **binary Variant tree** (``Scripts.loadDataPack``
                      / ``.pbd`` and ``.psd`` layer-info data).  Not bytecode!
``TJS/4s0\\0``         Same Variant tree, wrapped in PackinOne crypt + framed
                      LZ4.  ``.pbd`` (PBD) files -- parsed by ``pbd.py``
                      (t-psb); this module only identifies the type.
===================  ==========================================================

TJS2100 layout (validated against every tenshi entry, matches
``vendor/.../src/common/tjs2_inspect.py`` and the vendor decompiler)::

    b'TJS2100\\0'  declared_size:u32  chunk{ tag[4], size:u32, body[size-8] }*

    DATA body:
        byte  pool : u32 count + i8  * count           (pad to 4)
        short pool : u32 count + i16 * count           (pad to 4)
        int   pool : u32 count + i32 * count
        i64   pool : u32 count + i64 * count
        real  pool : u32 count + f64 * count
        string pool: u32 count + { u32 char_count + utf16le chars (pad 4) }*
        octet pool : u32 count + { u32 byte_count + bytes        (pad 4) }*

    OBJS body:
        i32 toplevel_object, u32 object_count,
        { b'TJS2'[4], size:u32, object[size] }*

TJS/ns0 layout (reverse engineered; cross-checked against the public Rust
reference implementation ``msg-tool/src/scripts/kirikiri/tjs_ns0.rs`` and
verified on all 115 tenshi files -- checksum and exact byte consumption)::

    header (16 bytes):
        magic  'TJS/'    check 'ns0\\0' | '4s0\\0'
        seed   u32       (on disk these bytes are literally b'TJS\\0')
        crypt  u16       (0 for ns0)
        iv_len u16       (0 for ns0)
    body: one Variant.  Every value starts with a *checked* u16 tag
          ``(check_byte << 8) | type_byte``; ``check_byte`` comes from a
          31-bit ByteChecker seeded with ``seed``:

        type 0x00  Void
        type 0x02  Str    : u32 char_count + utf16le chars  (NO padding)
        type 0x04  Int    : i64
        type 0x05  Double : f64
        type 0x81  Array  : u32 count + Variant*
        type 0xC1  Dict   : u32 count + { u32 key_len + utf16le key + Variant }*
    trailer: u32 checksum (ByteChecker.final_check()).

Public API (consumed by ``hxlib.pipeline``)
-------------------------------------------
``KINDS``
    corpus kinds this module can turn into Facts: ``('tjs', 'tjs_ns')``.
``iter_facts(entry, data)``
    Yields :class:`hxlib.facts.Fact` objects for one decrypted entry.
``parse(data)`` / ``sniff(data)``
    Container parsing (debug / other modules).
``decompile(data)``
    TJS2100 -> TJS source (wrapper around the vendor decompiler).
``decompile_corpus(entries, game, ...)``
    Writes ``work/decompiled/<game>/<hash>.tjs`` + ``index.jsonl``.
"""
from __future__ import annotations

import json
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

# --------------------------------------------------------------------------
# Fact interface (Lead's lib/hxlib/facts.py).  Import lazily / defensively so
# this module can also be used stand-alone by the probe scripts.
# --------------------------------------------------------------------------
try:  # pragma: no cover - import path is exercised by the pipeline
    from ..facts import Fact, FactWriter, KIND_DIR, KIND_FRAGMENT, KIND_NAME
except Exception:  # pragma: no cover
    try:
        from hxlib.facts import (Fact, FactWriter, KIND_DIR, KIND_FRAGMENT,
                                 KIND_NAME)
    except Exception:
        KIND_NAME, KIND_DIR, KIND_FRAGMENT = 'NAME', 'DIR', 'FRAGMENT'

        @dataclass(frozen=True)
        class Fact:  # type: ignore[no-redef]
            value: str
            kind: str = KIND_NAME
            source: str = ''
            note: str = ''
            context: str = ''

        class FactWriter:  # type: ignore[no-redef]
            def __init__(self, *a, **k):
                raise RuntimeError('hxlib.facts not importable')

# corpus kind names
T_TJS = 'tjs'          # TJS2100 bytecode
T_TJS_NS = 'tjs_ns'    # TJS/ns0 serialized Variant tree
T_PBD = 'pbd'          # TJS/4s0 (handled by pbd.py)

KINDS: tuple[str, ...] = (T_TJS, T_TJS_NS)

MAGIC_TJS2100 = b'TJS2100\x00'
MAGIC_NS0 = b'TJS/ns0\x00'
MAGIC_4S0 = b'TJS/4s0\x00'

# root of the project (lib/hxlib/formats/tjs.py -> project root)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DECOMPILER_DIR = (PROJECT_ROOT / 'vendor' / 'cxdec-hxv4-static-analysis'
                  / 'tools' / 'tjs2-decompiler')

# --------------------------------------------------------------------------
# Evidence-backed extension set used to classify a bare string as a *complete*
# file name.  Every extension here is either observed in the decompiled TJS
# sources of the game itself or is a container this tool understands:
#   .tjs  KAGLoadScript("x.tjs") / include(scriptBase + "...tjs")
#   .ks   setSystemMenuStorage("x") -> "x.ks"; SystemScript
#   .pbd  GetUIImport(name) -> name_<lang>.pbd / name.pbd
#   .tlg/.png/...  global.Layer.SupportedExtensions (utils.tjs:70)
#   .ogg/.opus/.wav  getExistVoice() (utils.tjs:515)
#   .mp4  SelectMovieFile (uisupport.tjs:79)
#   .asd  affinesource*: chopStorageExt(x) + ".asd"
#   .sinfo/.stand/.stage/.event  _imageFileExtList (kagenvironment.tjs:105)
#   .txt/.csv/.ini/.toml/.yaml/.json/.func  data tables referenced by scripts
# --------------------------------------------------------------------------
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.tlg', '.pimg', '.tlg5', '.tlg6',
              '.bmp', '.psd', '.webp', '.pbd')
AUDIO_EXTS = ('.ogg', '.opus', '.wav', '.mp3', '.m4a')
SCRIPT_EXTS = ('.tjs', '.ks')
DATA_EXTS = ('.txt', '.csv', '.ini', '.toml', '.yaml', '.json', '.func',
             '.sinfo', '.stand', '.stage', '.event', '.sli', '.vol.csv',
             '.asd', '.mtn', '.motion', '.psb', '.pimg', '.tft')
OTHER_EXTS = ('.dll', '.exe', '.xp3', '.mp4', '.wmv', '.mov', '.amv', '.mpeg',
              '.m2v', '.mtn', '.log', '.sav', '.srd', '.bin', '.dat')
KNOWN_EXTS = frozenset(IMAGE_EXTS + AUDIO_EXTS + SCRIPT_EXTS + DATA_EXTS
                       + OTHER_EXTS)


# ==========================================================================
# Container detection
# ==========================================================================
def sniff(data: bytes) -> str | None:
    """Return ``'tjs'`` (TJS2100), ``'tjs_ns'`` (TJS/ns0) or ``None``."""
    if not data:
        return None
    if data.startswith(MAGIC_TJS2100):
        return T_TJS
    if data.startswith(MAGIC_NS0):
        return T_TJS_NS
    return None


def container_type(data: bytes) -> str:
    """Human readable container type (``'tjs2100'``/``'tjs_ns0'``/``'tjs_4s0'``/``'unknown'``)."""
    if data.startswith(MAGIC_TJS2100):
        return 'tjs2100'
    if data.startswith(MAGIC_NS0):
        return 'tjs_ns0'
    if data.startswith(MAGIC_4S0):
        return 'tjs_4s0'
    return 'unknown'


def parse(data: bytes) -> dict[str, Any]:
    """Parse either TJS container.  Returns a dict with a ``type`` key."""
    kind = sniff(data)
    if kind == T_TJS:
        return {'type': 'tjs2100', **parse_tjs2100(data)}
    if kind == T_TJS_NS:
        return {'type': 'tjs_ns0', **parse_tjs_ns0(data)}
    raise TjsError('not a TJS container: %r' % (data[:8],))


class TjsError(ValueError):
    """Base error for malformed TJS containers."""


# ==========================================================================
# TJS2100
# ==========================================================================
class Tjs2100Error(TjsError):
    pass


def _u32(d: bytes, off: int) -> int:
    return struct.unpack_from('<I', d, off)[0]


def _i32(d: bytes, off: int) -> int:
    return struct.unpack_from('<i', d, off)[0]


def _align4(v: int) -> int:
    return (v + 3) & ~3


@dataclass
class Tjs2100File:
    """Parsed TJS2100 bytecode container."""

    declared_size: int
    size: int
    chunks: list[dict]
    strings: list[str] = field(default_factory=list)
    octets: list[bytes] = field(default_factory=list)
    bytecode_literals: list[int] = field(default_factory=list)
    shorts: list[int] = field(default_factory=list)
    ints: list[int] = field(default_factory=list)
    int64s: list[int] = field(default_factory=list)
    reals: list[float] = field(default_factory=list)
    data_parsed_size: int = 0
    object_count: int = 0
    objects: list[dict] = field(default_factory=list)
    error: str = ''


def parse_chunks(data: bytes) -> list[dict]:
    if len(data) < 12 or not data.startswith(MAGIC_TJS2100):
        raise Tjs2100Error('not a TJS2100 file')
    declared = _u32(data, 8)
    chunks: list[dict] = []
    off = 12
    while off + 8 <= len(data):
        tag = data[off:off + 4].decode('ascii', 'replace')
        size = _u32(data, off + 4)
        if size < 8 or off + size > len(data):
            raise Tjs2100Error('chunk %r at %#x: size %#x exceeds file size %#x'
                               % (tag, off, size, len(data)))
        chunks.append({'tag': tag, 'offset': off, 'size': size,
                       'body': data[off + 8:off + size]})
        off += size
    return chunks


def parse_data_chunk(body: bytes) -> dict[str, Any]:
    """Parse the ``DATA`` chunk (literal pools) of a TJS2100 file."""
    data = body
    p = 0
    pools: dict[str, Any] = {}

    def take_count(unit: int) -> list:
        nonlocal p
        if p + 4 > len(data):
            raise Tjs2100Error('pool header truncated at %#x' % p)
        count = _u32(data, p)
        p += 4
        size = count * unit
        raw = data[p:p + size]
        if len(raw) < size:
            raise Tjs2100Error('pool truncated (count=%d unit=%d at %#x)'
                               % (count, unit, p))
        p += _align4(size)
        if unit == 1:
            return list(raw)
        fmt = {2: '<%dh', 4: '<%di', 8: '<%dq'}[unit] % count
        return list(struct.unpack(fmt, raw)) if count else []

    pools['bytecode_literals'] = take_count(1)
    pools['shorts'] = take_count(2)
    pools['ints'] = take_count(4)
    pools['int64s'] = take_count(8)

    real_count = _u32(data, p)
    p += 4
    pools['reals'] = (list(struct.unpack_from('<%dd' % real_count, data, p))
                      if real_count else [])
    p += 8 * real_count

    strings: list[str] = []
    if p + 4 > len(data):
        raise Tjs2100Error('string pool header truncated at %#x' % p)
    string_count = _u32(data, p)
    p += 4
    for _ in range(string_count):
        if p + 4 > len(data):
            raise Tjs2100Error('string length truncated at %#x' % p)
        length = _u32(data, p)
        p += 4
        raw = data[p:p + 2 * length]
        if len(raw) < 2 * length:
            raise Tjs2100Error('string data truncated at %#x' % p)
        p += _align4(2 * length)
        strings.append(raw.decode('utf-16le', errors='replace'))
    pools['strings'] = strings

    octets: list[bytes] = []
    if p + 4 <= len(data):
        octet_count = _u32(data, p)
        p += 4
        for _ in range(octet_count):
            if p + 4 > len(data):
                raise Tjs2100Error('octet length truncated at %#x' % p)
            length = _u32(data, p)
            p += 4
            octets.append(data[p:p + length])
            p += _align4(length)
    pools['octets'] = octets
    pools['parsed_size'] = p
    pools['declared_size'] = len(data)
    return pools


def parse_objs_chunk(body: bytes) -> dict[str, Any]:
    """Parse the ``OBJS`` chunk header (object table)."""
    result: dict[str, Any] = {'toplevel': None, 'object_count': 0, 'objects': []}
    if len(body) < 8:
        return result
    result['toplevel'] = _i32(body, 0)
    result['object_count'] = _u32(body, 4)
    p = 8
    for index in range(result['object_count']):
        if p + 8 > len(body):
            break
        tag = body[p:p + 4].decode('ascii', 'replace')
        size = _u32(body, p + 4)
        body_end = p + 8 + size
        if body_end > len(body):
            raise Tjs2100Error('object[%d] overruns OBJS chunk' % index)
        rec: dict[str, Any] = {'index': index, 'tag': tag, 'size': size}
        obj = body[p + 8:body_end]
        if tag != 'TJS2':
            raise Tjs2100Error('object[%d] bad tag %r' % (index, tag))
        if len(obj) >= 0x30:
            (rec['parent'], rec['name_idx'], rec['context_type'],
             rec['max_variable_count'], rec['variable_reserve_count'],
             rec['max_frame_count'], rec['func_decl_arg_count'],
             rec['func_decl_unnamed_arg_array_base'],
             rec['func_decl_collapse_base'], rec['prop_setter'],
             rec['prop_getter'], rec['super_class_getter']) = \
                struct.unpack_from('<12i', obj, 0)
        result['objects'].append(rec)
        p = body_end
    result['parsed_size'] = p
    return result


def parse_tjs2100(data: bytes) -> Tjs2100File:
    """Parse a TJS2100 container into a :class:`Tjs2100File`."""
    chunks = parse_chunks(data)
    out = Tjs2100File(declared_size=_u32(data, 8), size=len(data),
                      chunks=[{'tag': c['tag'], 'offset': c['offset'],
                               'size': c['size']} for c in chunks])
    for chunk in chunks:
        if chunk['tag'] == 'DATA':
            pools = parse_data_chunk(chunk['body'])
            out.strings = pools['strings']
            out.octets = pools['octets']
            out.bytecode_literals = pools['bytecode_literals']
            out.shorts = pools['shorts']
            out.ints = pools['ints']
            out.int64s = pools['int64s']
            out.reals = pools['reals']
            out.data_parsed_size = pools['parsed_size']
        elif chunk['tag'] == 'OBJS':
            objs = parse_objs_chunk(chunk['body'])
            out.object_count = objs['object_count']
            out.objects = objs['objects']
    return out


# ==========================================================================
# TJS/ns0  (serialized TJS2 Variant tree)
# ==========================================================================
class TjsNs0Error(TjsError):
    pass


class _ByteChecker:
    """The ``tjs2 byte checker`` sequence used for the per-value tag bytes.

    Reproduces ``ByteChecker`` from KiriKiri's TJS2 source (``tjsNative``/
    ``tjsDataPack``).  ``get_seed(type)`` returns the expected high byte for
    the next tag and advances the 24-bit state; ``final_check()`` produces
    the trailing file checksum.
    """

    __slots__ = ('seed',)

    def __init__(self, seed: int) -> None:
        self.seed = seed & 0xFFFFFFFF

    @staticmethod
    def _round(s: list[int]) -> None:
        a = (s[0] ^ ((s[0] * 2) & 0xFF)) & 0xFF
        b = (a >> 2) & 0xFF
        b = (b ^ s[2]) & 0xFF
        b = (b >> 3) & 0xFF
        b = (b ^ s[2]) & 0xFF
        b = (b ^ a) & 0xFF
        s[0], s[1], s[2] = s[1], s[2], b

    def get_seed(self, type_code: int) -> int:
        s = list(struct.pack('<I', self.seed))
        if type_code == 0:
            return s[2]
        self._round(s)
        self.seed = struct.unpack('<I', bytes(s))[0]
        return s[2]

    def final_check(self) -> int:
        s = list(struct.pack('<I', self.seed))
        self._round(s)
        self._round(s)
        self._round(s)
        s[0], s[2] = s[2], s[0]
        return struct.unpack('<I', bytes(s))[0]


class _Reader:
    __slots__ = ('data', 'pos')

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def take(self, n: int) -> bytes:
        end = self.pos + n
        if end > len(self.data):
            raise TjsNs0Error('truncated at %#x (want %d bytes)' % (self.pos, n))
        out = self.data[self.pos:end]
        self.pos = end
        return out

    def u16(self) -> int:
        return struct.unpack('<H', self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack('<I', self.take(4))[0]

    def i64(self) -> int:
        return struct.unpack('<q', self.take(8))[0]

    def f64(self) -> float:
        return struct.unpack('<d', self.take(8))[0]

    def utf16(self) -> str:
        n = self.u32()
        if n > len(self.data) // 2:
            raise TjsNs0Error('string length %d out of range at %#x'
                              % (n, self.pos - 4))
        raw = self.take(2 * n)
        return raw.decode('utf-16le', errors='replace')


@dataclass
class TjsNs0File:
    """Parsed TJS/ns0 container: a TJS2 Variant tree (PBD / data pack)."""

    seed: int
    crypt: int
    iv_len: int
    tree: Any
    consumed: int
    size: int
    checksum_ok: bool
    trailer: int | None = None
    error: str = ''


def parse_tjs_ns0(data: bytes) -> TjsNs0File:
    """Parse a TJS/ns0 serialized Variant tree (``.pbd`` data pack)."""
    if not data.startswith(MAGIC_NS0):
        raise TjsNs0Error('not a TJS/ns0 file')
    if len(data) < 16:
        raise TjsNs0Error('short header (%d bytes)' % len(data))
    magic, check, seed = struct.unpack_from('<4s4sI', data, 0)
    if magic != b'TJS/' or check != b'ns0\0':
        raise TjsNs0Error('bad magic/check %r %r' % (magic, check))
    crypt, iv_len = struct.unpack_from('<HH', data, 12)
    if crypt != 0 or iv_len != 0:
        raise TjsNs0Error('crypt/iv unsupported (crypt=%d iv=%d)' % (crypt, iv_len))
    reader = _Reader(data, 16)
    checker = _ByteChecker(seed)
    tree = _read_variant(reader, checker)
    trailer = None
    checksum_ok = False
    if reader.pos + 4 <= len(data):
        trailer = reader.u32()
        checksum_ok = checker.final_check() == trailer
    return TjsNs0File(seed=seed, crypt=crypt, iv_len=iv_len, tree=tree,
                      consumed=reader.pos, size=len(data),
                      checksum_ok=checksum_ok, trailer=trailer)


def _read_variant(reader: _Reader, checker: _ByteChecker) -> Any:
    tag = reader.u16()
    type_byte = tag & 0xFF
    check_byte = (tag >> 8) & 0xFF
    expected = checker.get_seed(type_byte)
    if check_byte != expected:
        raise TjsNs0Error('byte check failed at %#x: type %#x expected %02x got %02x'
                          % (reader.pos - 2, type_byte, expected, check_byte))
    if type_byte == 0x00:          # Void
        return None
    if type_byte == 0x02:          # String
        return reader.utf16()
    if type_byte == 0x04:          # Int (i64)
        return reader.i64()
    if type_byte == 0x05:          # Double
        return reader.f64()
    if type_byte == 0x81:          # Array
        n = reader.u32()
        if n > len(reader.data):
            raise TjsNs0Error('array count %d out of range at %#x'
                              % (n, reader.pos - 4))
        return [_read_variant(reader, checker) for _ in range(n)]
    if type_byte == 0xC1:          # Dict
        n = reader.u32()
        if n > len(reader.data):
            raise TjsNs0Error('dict count %d out of range at %#x'
                              % (n, reader.pos - 4))
        out: dict[str, Any] = {}
        for _ in range(n):
            key = reader.utf16()
            out[key] = _read_variant(reader, checker)
        return out
    raise TjsNs0Error('unsupported value type %#x at %#x'
                      % (type_byte, reader.pos - 2))


# --------------------------------------------------------------------------
# Variant tree walking
# --------------------------------------------------------------------------
def walk_variant(tree: Any, path: str = '', key: str = '') -> Iterator[tuple[str, str, str]]:
    """Yield ``(json_path, parent_key, string_value)`` for every string leaf."""
    if isinstance(tree, dict):
        for k, v in tree.items():
            sub = path + '/' + k
            yield from walk_variant(v, sub, k)
    elif isinstance(tree, list):
        for i, v in enumerate(tree):
            yield from walk_variant(v, '%s[%d]' % (path, i), key)
    elif isinstance(tree, str):
        yield path, key, tree


def all_strings(data: bytes) -> list[str]:
    """Return every string literal in a TJS2100 or TJS/ns0 file."""
    kind = sniff(data)
    if kind == T_TJS:
        return list(parse_tjs2100(data).strings)
    if kind == T_TJS_NS:
        return [s for _p, _k, s in walk_variant(parse_tjs_ns0(data).tree)]
    return []


# ==========================================================================
# Fact extraction
# ==========================================================================
# keys of the TJS/ns0 Variant tree that hold *file references* (evidence:
# decompiled game scripts load these through Storages/GetUIImport/loadImages/
# getExistPSDImageName; see logic.engine_rules()).
# ``storage``/``holdimage`` are image-pack names (+".tlg"), ``packbase`` is the
# PBD's own stem (+".pbd"), ``layerFilename`` is an image stem (+image ext),
# ``sename`` is a sound-effect stem (+audio ext).
PBD_NAME_KEYS = frozenset({
    'storage', 'layerFilename', 'filename', 'file', 'file_name',
    'sename', 'se', 'packbase', 'holdimage',
})
#: keys whose values are TJS code fragments (``&GetUIImport("x_locale")``);
#: string literals are extracted from them instead of the whole value
PBD_CODE_KEYS = frozenset({'imports', 'exp', 'onchangefunc', 'onenter',
                           'onleave', 'pre', 'value', 'elm'})

#: Storage-name prefixes of *generated thumbnail* entries.  Their exact
#: suffixes are proven by the game's own cache generators:
#:   chthum_<char>_<16hex>.png   uistand.tjs: "chthum_%s_%08x%08x.png"
#:   bgthum_<stage>.jpg          uistand.tjs getThumbCacheRealFile():
#:                               return "bgthum_" + arg0 + "." + arg3 (arg3="jpg")
#:   thum_* / savethum_*         CG-mode / save-screen thumbnail tables
#: The ``_chthum_index.pbd`` data pack stores the chthum stems verbatim as its
#: values (442 of them), so the data pack itself is the enumeration source.
PBD_THUMB_PREFIXES = ('chthum_', 'bgthum_', 'thum_', 'savethum_')

_IMPORT_CALL_RE = re.compile(
    r'(?:GetUIImport|GetUIFile|GetUIPSD|GetUIImage)\s*\(\s*'
    r'(["\'])([^"\']+)\1')

# Characters that cannot appear in a real (Windows) storage name; TJS code
# fragments and regexes always trip at least one of these.
_STORAGE_BAD = set('\\/:*?"<>|\r\n\t\x00')
_CODE_HINTS = ('(', ')', '{', '}', ';', ' = ', '==', '=>', '&&', '||', '%[',
               '.cmd(', '.func(', 'Current.', 'typeof ', 'function')


def _has_known_ext(s: str) -> str | None:
    """Return the matched lower-case extension if ``s`` ends with one."""
    lo = s.lower()
    for ext in KNOWN_EXTS:
        if lo.endswith(ext):
            # a 5-char name part at minimum ("a.png" is not a real game name)
            if len(s) > len(ext) + 1:
                return ext
    return None


def _is_storage_chars(s: str) -> bool:
    """No Windows-invalid chars, no whitespace except a plain space."""
    for ch in s:
        if ch in _STORAGE_BAD:
            return False
        if ch in '\x00\r\n\t':
            return False
    return True


def _looks_like_code(s: str) -> bool:
    return any(hint in s for hint in _CODE_HINTS)


def classify_string(s: str) -> tuple[str, str] | None:
    """Classify a bare string as ``(kind, note)`` or ``None`` when unusable.

    * complete file name (known extension) -> ``NAME``
    * directory (trailing ``/``)           -> ``DIR``
    * path-ish fragment (has ``/``)        -> ``FRAGMENT``
    """
    if not s or len(s) > 200:
        return None
    if s != s.strip():
        return None
    if _looks_like_code(s):
        return None
    if s.endswith('/'):
        body = s[:-1]
        if not body or body.startswith('/') or '//' in s or '.' in body:
            return None
        if not _is_storage_chars(body):
            return None
        if not any(ch.isalnum() or ch == '_' for ch in body):
            return None
        return KIND_DIR, 'directory literal'
    ext = _has_known_ext(s)
    if ext is not None:
        if not _is_storage_chars(s):
            return None
        return KIND_NAME, 'file name (known extension %s)' % ext
    if '/' in s and not s.startswith('/') and '//' not in s:
        if not _is_storage_chars(s):
            return None
        if any(ch in '(){};=%' for ch in s):
            return None
        return KIND_FRAGMENT, 'path fragment'
    return None


def _fact(value: str, kind: str, source: str, note: str, context: str) -> Fact:
    return Fact(value=value, kind=kind, source=source, note=note,
                context=context)


def _tjs2100_facts(entry, data: bytes) -> Iterator[Fact]:
    parsed = parse_tjs2100(data)
    src = 'tjs:%s#%s' % (getattr(entry, 'archive', '?'),
                         getattr(entry, 'index', '?'))
    for i, s in enumerate(parsed.strings):
        cls = classify_string(s)
        if cls is None:
            continue
        kind, note = cls
        yield _fact(s, kind, '%s:strings[%d]' % (src, i), note,
                    'tjs:stringpool')
    # octet literals occasionally carry embedded text; not used for names.


def _ns0_facts(entry, data: bytes) -> Iterator[Fact]:
    parsed = parse_tjs_ns0(data)
    src = 'pbd:%s#%s' % (getattr(entry, 'archive', '?'),
                         getattr(entry, 'index', '?'))
    for json_path, parent_key, value in walk_variant(parsed.tree):
        if not value or len(value) > 200 or value != value.strip():
            continue
        if parent_key in PBD_CODE_KEYS:
            # TJS code fragment stored in the PBD (e.g. the "imports" list
            # ``&GetUIImport("option_locale")``): keep the string literals of
            # the UI-file loaders, they are storage stems.  The context is
            # ``tjs:ui.pbd`` because that is the call the fragment performs
            # (GetUIImport -> GetUIFile(base, ".pbd", lang) -> base_<lang>.pbd).
            for m in _IMPORT_CALL_RE.finditer(value):
                stem = m.group(2)
                if len(stem) < 2 or not _is_storage_chars(stem):
                    continue
                yield _fact(stem, KIND_FRAGMENT,
                            '%s%s[%s]' % (src, json_path, m.group(0)[:40]),
                            'PBD %r code: %s' % (parent_key, m.group(0)[:60]),
                            'tjs:ui.pbd')
            continue
        if _looks_like_code(value):
            continue
        ext = _has_known_ext(value)
        if ext is not None and _is_storage_chars(value):
            yield _fact(value, KIND_NAME, '%s%s' % (src, json_path),
                        'value with known extension %s' % ext, 'pbd:filename')
            continue
        if parent_key in PBD_NAME_KEYS:
            if not _is_storage_chars(value) or value.endswith('/'):
                continue
            if len(value) < 2:
                continue
            # contexts follow hxlib.rules.CONTEXT_RULES:
            #   pbd:holdimage / pbd:packbase / pbd:storage / pbd:layerFilename /
            #   pbd:sename
            yield _fact(value, KIND_FRAGMENT, '%s%s' % (src, json_path),
                        'PBD %r value' % parent_key,
                        'pbd:%s' % parent_key)
            continue
        # Generated-thumbnail stems stored verbatim in a data pack (the value
        # lives under arbitrary keys, so this is driven by the value prefix).
        # ``_chthum_index.pbd`` is the character-thumbnail index: its values are
        # exactly ``chthum_<char>_<16hex>`` (the game appends ".png"); the
        # prefix conventions are proven in uistand.tjs (see module docstring).
        for prefix in PBD_THUMB_PREFIXES:
            if value.startswith(prefix) and len(value) > len(prefix):
                if not _is_storage_chars(value) or _looks_like_code(value):
                    break
                yield _fact(value, KIND_FRAGMENT, '%s%s' % (src, json_path),
                            'generated thumbnail stem (data pack)',
                            'pbd:thumb')
                break


def iter_facts(entry, data: bytes) -> Iterator[Fact]:
    """Yield Facts for one decrypted corpus entry (TJS2100 or TJS/ns0).

    ``entry`` is a :class:`hxlib.corpus.CorpusEntry` (``.hash/.archive/.index/
    .kind``); only its provenance fields are used, the container type comes
    from the bytes themselves.  Facts are *not* hash-verified here -- that is
    ``verify.py``'s job.

    Every ``NAME``/``DIR`` fact is emitted together with a lower-case variant:
    the XP3 entries of this engine family are stored with fully lower-case
    names (all 57,748 names of the tenshi community oracle are lower-case,
    including the ones the scripts load as ``KAGLoadScript("AffineMatrix.tjs")``).
    """
    kind = sniff(data)
    if kind is None:
        return
    expected = getattr(entry, 'kind', None)
    if expected is not None and expected not in (T_TJS, T_TJS_NS):
        return
    if kind == T_TJS:
        facts = _tjs2100_facts(entry, data)
    else:
        facts = _ns0_facts(entry, data)
    for fact in facts:
        yield fact
        if fact.kind not in (KIND_NAME, KIND_DIR):
            continue
        low = fact.value.lower()
        if low != fact.value:
            yield _fact(low, fact.kind, fact.source,
                        'lower-case storage variant: ' + fact.note,
                        fact.context + '.lower')


# ==========================================================================
# Decompilation (vendor TJS2 decompiler wrapper)
# ==========================================================================
class DecompileError(RuntimeError):
    pass


_DECOMPILER_STATE: dict[str, Any] = {}


def _load_decompiler():
    """Import the vendored TJS2 decompiler (``CFGDecompiler``) lazily."""
    if _DECOMPILER_STATE.get('mod') is not None:
        return _DECOMPILER_STATE['mod']
    if not DECOMPILER_DIR.is_dir():
        raise DecompileError('vendor decompiler not found: %s' % DECOMPILER_DIR)
    d = str(DECOMPILER_DIR)
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import importlib
        dec = importlib.import_module('tjs2_decompiler')
        cfg = importlib.import_module('tjs2_cfg_decompiler')
        fmt = importlib.import_module('tjs2_formatting')
    except Exception as ex:  # pragma: no cover
        raise DecompileError('cannot import vendor decompiler: %s: %s'
                             % (type(ex).__name__, ex))
    _DECOMPILER_STATE['mod'] = (dec, cfg.CFGDecompiler, fmt.format_source)
    return _DECOMPILER_STATE['mod']


def decompile(data: bytes) -> str:
    """Decompile TJS2100 bytecode to TJS2 source.

    Raises :class:`DecompileError` with a human readable reason.  The return
    value is ``''`` only for a structurally valid but empty script.
    """
    if not data.startswith(MAGIC_TJS2100):
        raise DecompileError('not TJS2100 bytecode')
    dec, cfg_decompiler, format_source = _load_decompiler()
    try:
        loader = dec.BytecodeLoader(data)
        if not loader.load():
            raise DecompileError('vendor loader rejected bytecode')
    except DecompileError:
        raise
    except Exception as ex:
        raise DecompileError('loader: %s: %s' % (type(ex).__name__, ex))
    try:
        source = cfg_decompiler(loader).decompile()
    except Exception as ex:
        raise DecompileError('decompile: %s: %s' % (type(ex).__name__, ex))
    try:
        source = format_source(source)
    except Exception:
        pass  # formatting is cosmetic; keep the raw source
    return source


def decompile_cache_paths(game: str, root: str | Path = 'work'):
    out_dir = Path(root) / 'decompiled' / game
    return out_dir, out_dir / 'index.jsonl'


def decompile_corpus(entries: Iterable[tuple[Any, bytes]], game: str,
                     *, root: str | Path = 'work', force: bool = False,
                     log=None) -> dict[str, Any]:
    """Decompile every TJS2100 entry and cache the sources.

    Parameters
    ----------
    entries
        Iterable of ``(corpus_entry, decrypted_bytes)``.  Only TJS2100
        (``entry.kind == 'tjs'`` / magic ``TJS2100``) is decompiled; other
        kinds are recorded in the index as skipped.
    game
        Game id used for the cache directory (``work/decompiled/<game>/``).
    force
        Re-decompile even when the cache file already exists.

    Returns
    -------
    dict with ``ok`` / ``failed`` / ``skipped`` counts, ``failed`` detail
    list and ``sources`` mapping ``hash -> source text`` (already-done cache
    hits read from disk).
    """
    out_dir, index_path = decompile_cache_paths(game, root)
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = failed = skipped = 0
    failures: list[dict] = []
    skipped_detail: list[dict] = []
    sources: dict[str, str] = {}
    index_rows: list[dict] = []

    for entry, data in entries:
        h = getattr(entry, 'hash', '') or ''
        archive = getattr(entry, 'archive', '')
        index = getattr(entry, 'index', -1)
        row = {'hash': h, 'archive': archive, 'index': index,
               'ok': False, 'error': '', 'source_path': '',
               'bytes': len(data) if data else 0}
        if not data or not data.startswith(MAGIC_TJS2100):
            skipped += 1
            row['error'] = 'not TJS2100 (%s)' % container_type(data or b'')
            skipped_detail.append(row)
            index_rows.append(row)
            continue
        dst = out_dir / ('%s.tjs' % h)
        if dst.is_file() and not force:
            try:
                sources[h] = dst.read_text(encoding='utf-8', errors='replace')
                ok += 1
                row['ok'] = True
                row['source_path'] = str(dst)
                row['cached'] = True
                index_rows.append(row)
                continue
            except OSError:
                pass
        try:
            source = decompile(data)
        except DecompileError as ex:
            failed += 1
            row['error'] = str(ex)
            failures.append(row)
            if log:
                log('  FAIL %s#%s %s: %s' % (archive, index, h[:12], ex))
            index_rows.append(row)
            continue
        except Exception as ex:  # defensive: vendor code can raise anything
            failed += 1
            row['error'] = '%s: %s' % (type(ex).__name__, ex)
            failures.append(row)
            index_rows.append(row)
            continue
        dst.write_text(source, encoding='utf-8')
        sources[h] = source
        ok += 1
        row['ok'] = True
        row['source_path'] = str(dst)
        index_rows.append(row)

    with index_path.open('w', encoding='utf-8') as fh:
        for row in index_rows:
            row.pop('bytes', None)
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')
    if log:
        log('decompiled: ok=%d failed=%d skipped=%d -> %s'
            % (ok, failed, skipped, index_path))
    return {'ok': ok, 'failed': failed, 'skipped': skipped,
            'failures': failures, 'skipped_detail': skipped_detail,
            'sources': sources, 'out_dir': str(out_dir),
            'index_path': str(index_path)}


def load_decompile_index(game: str, root: str | Path = 'work') -> list[dict]:
    """Read ``work/decompiled/<game>/index.jsonl``."""
    _out_dir, index_path = decompile_cache_paths(game, root)
    rows: list[dict] = []
    if not index_path.is_file():
        return rows
    with index_path.open(encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


# ==========================================================================
# CLI (debug helper; the real entry point is run.py)
# ==========================================================================
def _main(argv: Sequence[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description='Inspect / decompile TJS containers')
    ap.add_argument('paths', nargs='+')
    ap.add_argument('--decompile', action='store_true',
                    help='decompile TJS2100 and print the source')
    args = ap.parse_args(argv)
    for path in args.paths:
        p = Path(path)
        data = p.read_bytes()
        ctype = container_type(data)
        print('%s: %s (%d bytes)' % (p.name, ctype, len(data)))
        if ctype == 'tjs2100':
            parsed = parse_tjs2100(data)
            print('  chunks=%s strings=%d octets=%d objects=%d'
                  % ([c['tag'] for c in parsed.chunks], len(parsed.strings),
                     len(parsed.octets), parsed.object_count))
            for s in parsed.strings:
                print('    %r' % s)
            if args.decompile:
                print(decompile(data))
        elif ctype == 'tjs_ns0':
            parsed_ns = parse_tjs_ns0(data)
            n = sum(1 for _ in walk_variant(parsed_ns.tree))
            print('  checksum_ok=%s consumed=%d/%d strings=%d'
                  % (parsed_ns.checksum_ok, parsed_ns.consumed,
                     parsed_ns.size, n))
            print(json.dumps(parsed_ns.tree, ensure_ascii=False)[:2000])
        elif ctype == 'tjs_4s0':
            print('  TJS/4s0 (encrypted PBD) -- parsed by hxlib.formats.pbd')
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
