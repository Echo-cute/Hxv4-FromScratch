"""logic.py -- derive file-name candidates from the game's own TJS2 script logic.

The decompiled TJS sources of the game are a naming-logic gold mine: they show
exactly how the engine (and the game-specific KAG extensions) *construct* the
storage names that end up as XP3 entries.  Instead of brute-forcing tokens, this
module statically evaluates those construction sites:

* ``Scripts.execStorage("system/Initialize.tjs")``
* ``KAGLoadScript("AffineMatrix.tjs")`` / ``KAGLoadScriptIfDebug(...)``
* ``include(scriptBase + ("k2compat_" + string(arg0) + ".tjs"))`` plus the
  ``Krkr2CompatUtils.require("modeless")`` call sites that feed ``arg0``
* ``Storages.addAutoPath("proxy://./")`` / ``addArchive("system")`` -> the
  archive / directory list of ``initialize.tjs``
* ``setSystemMenuStorage(local0 + ".ks")`` / ``setSystemMenuStorage("search.ks")``
* ``GetUIFile(base, ".pbd", langTag)`` -> ``<base>_<langTag>.pbd``
* ``getExistImageName(x)`` / ``global.Layer.SupportedExtensions`` -> image suffix
  fallback lists; ``getExistVoice(x)`` -> ``.ogg``/``.opus``/``.wav``
* KAG command dictionaries: ``se.play(%[storage: arg0])``, ``bgm.play(%[storage: ...])``

How it works
------------
Every source is tokenised (strings / comments / regex literals are handled) and
split into ``file scope`` + one scope per ``function``.  Inside each scope all
assignments are collected and resolved to *sets of concrete strings* by a small
constant-propagation pass (literals, concatenation, ternary, ``string()``,
literal arrays / dicts, indexing into literal arrays, ``chopStorageExt`` ...).
Calls of the known file APIs are then resolved argument-by-argument.

Wrapper functions (``addArchive``, ``include``, ``require``, ...) are handled by
a second pass: for every function we extract *templates* of the API calls inside
its body (literal parts kept, parameters replaced by ``\\x00<i>\\x00``).  A call
site of such a function with resolvable arguments instantiates the templates,
so ``addArchive("system")`` is resolved through
``function addArchive(arg0) { Storages.addAutoPath(arg0 + "/"); ... }`` into the
DIR fact ``system/``.

Facts are produced for the *strings the game itself uses*.  No hash checking and
no extension guessing happens here beyond the suffix conventions that are
themselves proven by the decompiled sources (see :func:`engine_rules`).

Public API
----------
``analyze_sources(sources)``
    ``{key: decompiled_source}`` -> iterator of :class:`hxlib.facts.Fact`.
``iter_facts(entry, data)``
    Single TJS2100 corpus entry -> decompile + analyse.
``engine_rules()``
    Evidence-backed naming conventions / extension lists (for ``rules.py``).
``last_report()``
    Statistics of the last :func:`analyze_sources` call (call sites per API,
    resolved counts, samples of unresolved templates).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Sequence

try:  # pragma: no cover
    from .formats import tjs as _tjs
    from .facts import Fact, KIND_DIR, KIND_FRAGMENT, KIND_NAME
except Exception:  # pragma: no cover - allow ``python lib/hxlib/logic.py``
    try:
        from hxlib.formats import tjs as _tjs  # type: ignore
        from hxlib.facts import Fact, KIND_DIR, KIND_FRAGMENT, KIND_NAME  # type: ignore
    except Exception:
        KIND_NAME, KIND_DIR, KIND_FRAGMENT = 'NAME', 'DIR', 'FRAGMENT'

        @dataclass(frozen=True)
        class Fact:  # type: ignore[no-redef]
            value: str
            kind: str = KIND_NAME
            source: str = ''
            note: str = ''
            context: str = ''

        class _FakeTjs:  # pragma: no cover
            KNOWN_EXTS: frozenset = frozenset()

        _tjs = _FakeTjs()  # type: ignore

PLACEHOLDER = '\x00%d\x00'
_PLACEHOLDER_RE = re.compile('\x00(\\d+)\x00')

# ---------------------------------------------------------------------------
# Evidence-backed conventions used to turn a resolved *storage stem* into the
# concrete candidate names.  Each entry points at the decompiled code that
# proves it; ``engine_rules()`` returns the same information as structured data.
# ---------------------------------------------------------------------------
IMAGE_EXTS = ('.png', '.jpg', '.jpeg', '.tlg', '.pimg', '.tlg5', '.tlg6',
              '.bmp', '.psd', '.webp', '.pbd')
AUDIO_EXTS = ('.ogg', '.opus', '.wav', '.tcw', '.m4a', '.mp3')
SCRIPT_EXTS = ('.tjs', '.ks')

#: ``initialize.tjs`` (data#1223) registers these archives / directories through
#: ``addArchive()`` -> ``Storages.addAutoPath(arg0 + "/")``.  Code that opens
#: ``"system/Initialize.tjs"`` therefore resolves against the archive root;
#: the *stored* entry name is the part after the autopath prefix.
AUTOPATH_PREFIXES = (
    'k2compat/', 'system/', 'sysscn/', 'video/', 'others/', 'rule/', 'sound/',
    'bgm/', 'fgimage/', 'bgimage/', 'scenario/', 'scn/', 'image/', 'voice/',
    'face/', 'init/', 'font/', 'sysse/', 'main/', 'evimage/', 'thum/',
    'uipsd/', 'motion/', 'motiondx/', 'emote/', 'emotedx/', 'bishamon/',
    'glsl/', 'locale/cn/', 'locale/tw/', 'locale/en/', 'locale/jp/',
    'uipsd/func/', 'scenario/scripts/', 'scenario/transitions/',
)

#: keys of KAG command dictionaries (``%[storage: "x"]``) and TJS data dicts
#: that carry *storage names*.  Evidence: ``se.tjs`` (data#1264) reads
#: ``arg0.storage``; ``kag.bgm.play(%[storage: ...])`` in world.tjs/others.
DICT_STORAGE_KEYS = frozenset({'storage', 'file', 'filename', 'file_name',
                               'bgm', 'se', 'voice', 'movie', 'image',
                               'graphic', 'graphics'})
_DICT_IMAGE_KEYS = frozenset({'image', 'graphic', 'graphics'})
_DICT_AUDIO_KEYS = frozenset({'bgm', 'se', 'voice', 'movie'})

# ---------------------------------------------------------------------------
# Language-tag driven naming (phase 2)
# ---------------------------------------------------------------------------
#: The game's language list.  Evidence: ``yuzu_default.tjs`` (a *text* entry,
#: data#502) line 410: ``.multiLangLanguageTags = [ "jp", "en", "cn", "tw" ];``
#: -- it overrides the multilang.tjs default ``["jp","en","cn"]`` (line 36).
#: ``GetSystemLangFile()`` (multilang.tjs data#1104 lines 1393-1413) turns
#: ``name.ext`` into ``name_<tag>.ext`` (falling back to ``name_en.ext``);
#: ``LoadSysLangText()`` (syslangtext.tjs data#1151 line 1) builds
#: ``syslangtext_<tag>.ini`` from the same list; ``uilangimage()`` /
#: ``GetUIImage(base, tag)`` (uisupport.tjs data#496 lines 116-120, 453-455)
#: builds ``<base>_<tag>`` and then auto-fills the image extension.
LANGUAGE_TAGS = ('jp', 'en', 'cn', 'tw')
NONJP_LANGS = ('en', 'cn', 'tw')

#: functions that resolve a file through ``GetSystemLangFile()`` (i.e. the
#: result may be ``<stem>_<lang><ext>``) -- see In ()/engine_rules().
LANG_FILE_HELPERS = frozenset({'GetSystemLangFile', 'getHelpTextFile',
                               'loadTranslateTable', 'uilangfile',
                               'meslangfile', 'meslangpbd',
                               'GetHelpTextFile'})
#: functions returning a *language-suffixed image* (``<base>_<lang>`` + ext).
LANG_IMAGE_HELPERS = frozenset({'uilangimage', 'meslangimage'})
#: functions returning ``<base>_<lang><ext>`` directly.
LANG_FORCE_HELPERS = frozenset({'uilangforce', 'meslangforce'})
#: k2compat lazy-loaders that expand to ``require('<name>')``
#: (k2compat.tjs data#224 lines 180-194) -> ``k2compat_<name>.tjs``.
K2COMPAT_DELAY = frozenset({'delayLoadFunction', 'delayLoadSingleton',
                            'delayLoadProperty'})
IMAGE_EXT_NAMES = ('.png', '.tlg', '.jpg')
THUMB_PREFIXES = ('chthum_', 'bgthum_', 'thum_', 'savethum_')
GLYPH_PREFIXES = ('linebreak', 'pagebreak', 'autoskipmark')

# ---------------------------------------------------------------------------
# API call rules.  ``name`` is matched against the *last* dotted component of
# the callee (``Scripts.execStorage`` -> ``execStorage``); ``full`` (optional)
# restricts to a specific dotted spelling.  ``arg`` selects the argument;
# ``kind`` is what the argument denotes:
#
#   'storage'  a storage name: complete name (known extension) -> NAME,
#              otherwise FRAGMENT with ``context`` (rules.py appends exts)
#   'dir'      a directory / auto-path -> DIR (``/`` appended)
#   'script'   a TJS/KAG script: ``.ks`` suffix added like setSystemMenuStorage
#   'plugin'   a plug-in dll: ``.dll`` suffix added when missing
#   'raw'      complete name or bare stem, no suffix convention
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ApiRule:
    name: str
    arg: int
    kind: str
    context: str
    note: str
    suffix: str = ''
    exts: tuple = ()


API_RULES: tuple[ApiRule, ...] = (
    ApiRule('execStorage', 0, 'storage', 'tjs:execStorage',
            'Scripts.execStorage(...)'),
    ApiRule('evalStorage', 0, 'storage', 'tjs:evalStorage',
            'Scripts.evalStorage(...)'),
    ApiRule('include', 0, 'storage', 'tjs:include',
            'include(...) (TJS include)'),
    ApiRule('KAGLoadScript', 0, 'storage', 'tjs:KAGLoadScript',
            'KAGLoadScript(...)'),
    ApiRule('KAGLoadScriptIfDebug', 0, 'storage', 'tjs:KAGLoadScript',
            'KAGLoadScriptIfDebug(...)'),
    ApiRule('addAutoPath', 0, 'dir', 'tjs:addAutoPath',
            'Storages.addAutoPath(...)'),
    ApiRule('setSystemMenuStorage', 0, 'script', 'tjs:setSystemMenuStorage',
            'setSystemMenuStorage(...) -- KAG appends ".ks"', suffix='.ks'),
    ApiRule('link', 0, 'plugin', 'tjs:Plugins.link',
            'Plugins.link(...) -- engine plug-in dll', suffix='.dll'),
    ApiRule('loadImages', 0, 'storage', 'tjs:image.storage',
            'Layer.loadImages(storage)'),
    ApiRule('loadImage', 0, 'storage', 'tjs:image.storage',
            'Layer.loadImage(storage)'),
    ApiRule('SelectMovieFile', 0, 'storage', 'tjs:movie.storage',
            'SelectMovieFile() -- .mp4/.wmv/.mtn + _censored variants'),
    ApiRule('getExistImageName', 0, 'storage', 'tjs:image.storage',
            'getExistImageName(x) -- auto-ext-fill image extensions',
            exts=IMAGE_EXTS),
    ApiRule('getExistImageNameForLayer', 0, 'storage', 'tjs:image.storage',
            'getExistImageNameForLayer(x)', exts=IMAGE_EXTS),
    ApiRule('getExistFileNameAutoExtFill', 0, 'storage', 'tjs:image.storage',
            'getExistFileNameAutoExtFill(x, exts...)', exts=IMAGE_EXTS),
    ApiRule('getExistPSDImageName', 0, 'storage', 'tjs:psd.storage',
            'getExistPSDImageName(x) -> .psd/.pimg/.pbd/.txt sidecars',
            exts=('.psd', '.pimg', '.pbd', '.txt')),
    ApiRule('getExistVoice', 0, 'storage', 'tjs:audio.storage',
            'getExistVoice(x) -> .ogg primary', exts=AUDIO_EXTS),
    ApiRule('getVoice', 0, 'storage', 'tjs:audio.storage',
            'getVoice(x)', exts=AUDIO_EXTS),
    ApiRule('GetUIImage', 0, 'storage', 'tjs:ui.image',
            'GetUIImage(base, layer) -> base + "_" + layer'),
    ApiRule('GetUIFile', 0, 'storage', 'tjs:ui.file',
            'GetUIFile(base, ext, langTag) -> base_<lang><ext>'),
    ApiRule('GetUIImport', 0, 'storage', 'tjs:ui.pbd',
            'GetUIImport(base) -> base_<lang>.pbd | base.pbd'),
    ApiRule('GetUIPSD', 0, 'storage', 'tjs:ui.psd',
            'GetUIPSD(base) -> base_<lang>.psd'),
    ApiRule('isExistentStorage', 0, 'raw', 'tjs:isExistentStorage',
            'Storages.isExistentStorage(name) -- explicit storage probe'),
    ApiRule('loadDataPack', 0, 'storage', 'tjs:datapack',
            'Scripts.loadDataPack(name)'),
    ApiRule('saveDataPack', 0, 'storage', 'tjs:datapack',
            'Scripts.saveDataPack(name)'),
    ApiRule('convertStorage', 0, 'storage', 'tjs:storage',
            'Storages.convertStorage(...)'),
)

_RULE_BY_NAME: dict[str, list[ApiRule]] = {}
for _rule in API_RULES:
    _RULE_BY_NAME.setdefault(_rule.name, []).append(_rule)

# Functions of the game itself that wrap a file API.  Calls of these are
# inlined through their recorded templates (``addArchive`` is defined in
# initialize.tjs, ``require``/``include`` in k2compat.tjs, ...).
WRAPPER_FUNCS = frozenset({
    'addArchive', 'addArchiveWithSubFolder', 'useArchiveIfExists',
    'require', 'include', 'GetUIFile', 'GetUIImport', 'GetUIImage',
    'getExistImageName', 'getExistVoice', 'loadImage', 'loadImages',
    'addAutoPath', 'addAutoPathRecursive', 'makeDelay', 'meslangpbd',
})

MAX_VALUES = 64          # cap per resolved expression
MAX_DEPTH = 6            # expression recursion depth
MAX_TEMPLATE_INSTANCES = 400


# ===========================================================================
# Tokeniser
# ===========================================================================
@dataclass(slots=True)
class Token:
    kind: str        # ident | num | str | regex | punct
    value: str
    start: int
    end: int


_STR_ESC = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f',
            'v': '\v', '0': '\0', '"': '"', "'": "'", '\\': '\\', '/': '/'}


def _unescape(body: str) -> str:
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        c = body[i]
        if c == '\\' and i + 1 < n:
            nx = body[i + 1]
            out.append(_STR_ESC.get(nx, nx))
            i += 2
            continue
        out.append(c)
        i += 1
    return ''.join(out)


def tokenize(src: str) -> list[Token]:
    """Tokenise decompiled TJS2 source (strings/comments/regex aware)."""
    tokens: list[Token] = []
    i = 0
    n = len(src)
    prev: Token | None = None
    while i < n:
        c = src[i]
        if c in ' \t\r\n':
            i += 1
            continue
        if c == '/' and i + 1 < n:
            if src[i + 1] == '/':
                j = src.find('\n', i + 2)
                i = n if j < 0 else j + 1
                continue
            if src[i + 1] == '*':
                j = src.find('*/', i + 2)
                i = n if j < 0 else j + 2
                continue
        if c in '"\'':
            j = i + 1
            while j < n:
                if src[j] == '\\':
                    j += 2
                    continue
                if src[j] == c:
                    break
                j += 1
            val = _unescape(src[i + 1:j])
            tok = Token('str', val, i, min(j + 1, n))
            tokens.append(tok)
            prev = tok
            i = min(j + 1, n)
            continue
        if c == '/' and _regex_allowed(prev):
            j = i + 1
            in_class = False
            while j < n:
                ch = src[j]
                if ch == '\\':
                    j += 2
                    continue
                if ch == '[':
                    in_class = True
                elif ch == ']':
                    in_class = False
                elif ch == '/' and not in_class:
                    break
                elif ch == '\n':
                    break
                j += 1
            val = src[i:j + 1]
            tok = Token('regex', val, i, j + 1)
            tokens.append(tok)
            prev = tok
            i = j + 1
            continue
        if c.isdigit() or (c == '.' and i + 1 < n and src[i + 1].isdigit()):
            j = i
            while j < n and (src[j].isalnum() or src[j] in '._'):
                j += 1
            tok = Token('num', src[i:j], i, j)
            tokens.append(tok)
            prev = tok
            i = j
            continue
        if c.isalpha() or c in '_$':
            j = i
            while j < n and (src[j].isalnum() or src[j] in '_$'):
                j += 1
            tok = Token('ident', src[i:j], i, j)
            tokens.append(tok)
            prev = tok
            i = j
            continue
        # punctuation (greedy on the common multi-char operators)
        for width in (3, 2):
            chunk = src[i:i + width]
            if chunk in ('===', '!==', '>>>', '<<=', '>>=', '...'):
                tok = Token('punct', chunk, i, i + width)
                tokens.append(tok)
                prev = tok
                i += width
                break
        else:
            chunk = src[i:i + 2]
            if chunk in ('==', '!=', '<=', '>=', '&&', '||', '++', '--',
                         '+=', '-=', '*=', '/=', '%=', '<<', '>>', '=>',
                         '::'):
                tok = Token('punct', chunk, i, i + 2)
                tokens.append(tok)
                prev = tok
                i += 2
            else:
                tok = Token('punct', c, i, i + 1)
                tokens.append(tok)
                prev = tok
                i += 1
    return tokens


def _regex_allowed(prev: Token | None) -> bool:
    if prev is None:
        return True
    if prev.kind == 'punct':
        return prev.value not in (')', ']', '}')
    if prev.kind == 'ident':
        return prev.value in ('return', 'typeof', 'case', 'in', 'of',
                              'delete', 'void', 'new', 'throw', 'instanceof')
    return False


def _match_bracket(tokens: Sequence[Token], open_idx: int) -> int:
    """Given the index of ``(``/``[``/``{``, return the index of its pair."""
    opener = tokens[open_idx].value
    closer = {'(': ')', '[': ']', '{': '}'}[opener]
    depth = 0
    for j in range(open_idx, len(tokens)):
        v = tokens[j].value
        if tokens[j].kind != 'punct':
            continue
        if v == opener:
            depth += 1
        elif v == closer:
            depth -= 1
            if depth == 0:
                return j
    return len(tokens) - 1


def _split_top(tokens: Sequence[Token], start: int, end: int,
               sep: str | tuple[str, ...]) -> list[tuple[int, int]]:
    """Split ``tokens[start:end]`` at depth-0 occurrences of ``sep``."""
    seps = (sep,) if isinstance(sep, str) else sep
    parts: list[tuple[int, int]] = []
    depth = 0
    part_start = start
    for j in range(start, end):
        tok = tokens[j]
        if tok.kind == 'punct':
            if tok.value in ('(', '[', '{'):
                depth += 1
            elif tok.value in (')', ']', '}'):
                depth -= 1
            elif depth == 0 and tok.value in seps:
                parts.append((part_start, j))
                part_start = j + 1
    parts.append((part_start, end))
    return parts


# ===========================================================================
# Expression resolution
# ===========================================================================
@dataclass
class Binding:
    name: str
    expr: tuple[int, int]          # token range of the RHS
    pos: int                       # token index of the LHS name
    line: int
    array_items: list[tuple[int, int]] | None = None
    dict_items: dict[str, tuple[int, int]] | None = None


@dataclass
class FunctionDef:
    name: str
    params: list[str]
    defaults: dict[str, tuple[int, int]]
    body_start: int                 # token index of '{'
    body_end: int                   # token index of '}'
    line: int
    scope: str


@dataclass
class CallSite:
    name: str                       # dotted callee name
    last: str                       # last component
    open_idx: int
    close_idx: int
    args: list[tuple[int, int]]
    pos: int
    line: int
    scope: str


class Scope:
    """Constant bindings of one lexical scope (file or function)."""

    def __init__(self, ident: str) -> None:
        self.ident = ident
        self.bindings: dict[str, list[Binding]] = {}
        self.values: dict[str, set[str]] = {}
        # per-binding resolved values: name -> [(token_pos, values)]
        self.binding_values: dict[str, list[tuple[int, set[str]]]] = {}
        # literal array bindings: name -> [(token_pos, [(start, end), ...])]
        self.arrays: dict[str, list[tuple[int, list[tuple[int, int]]]]] = {}
        self.dicts: dict[str, dict[str, tuple[int, int]]] = {}
        self.params: set[str] = set()

    def add_binding(self, b: Binding) -> None:
        self.bindings.setdefault(b.name, []).append(b)
        if b.array_items is not None:
            self.arrays.setdefault(b.name, []).append((b.pos, b.array_items))
        if b.dict_items is not None:
            self.dicts.setdefault(b.name, b.dict_items)

    def lookup(self, name: str) -> set[str] | None:
        vals = self.values.get(name)
        return vals if vals else None

    def lookup_at(self, name: str, pos: int) -> set[str] | None:
        """Flow-sensitive-ish lookup: prefer the last assignment before ``pos``."""
        entries = self.binding_values.get(name)
        if entries:
            before = [e for e in entries if e[0] < pos]
            if before:
                return max(before, key=lambda e: e[0])[1]
            return entries[0][1]
        return self.lookup(name)


class SourceFile:
    """Tokenised source + scopes + calls."""

    def __init__(self, key: str, src: str) -> None:
        self.key = key
        self.src = src
        self.tokens = tokenize(src)
        self.line_starts = [0]
        for m in re.finditer('\n', src):
            self.line_starts.append(m.start() + 1)
        self.functions: list[FunctionDef] = []
        self.param_ranges: list[tuple[int, int]] = []
        self.calls: list[CallSite] = []
        self.file_scope = Scope('file')
        self.scopes: dict[str, Scope] = {'file': self.file_scope}
        self._build_functions()
        self._assign_scopes()
        self._collect_bindings()

    # -- line numbers --------------------------------------------------
    def line_of(self, pos: int) -> int:
        lo, hi = 0, len(self.line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.line_starts[mid] <= pos:
                lo = mid
            else:
                hi = mid - 1
        return lo + 1

    def line_of_token(self, idx: int) -> int:
        toks = self.tokens
        if not toks:
            return 1
        idx = max(0, min(idx, len(toks) - 1))
        return self.line_of(toks[idx].start)

    def snippet(self, line: int, width: int = 160) -> str:
        lines = self.src.splitlines()
        if 1 <= line <= len(lines):
            return lines[line - 1].strip()[:width]
        return ''

    # -- structure -----------------------------------------------------
    def _build_functions(self) -> None:
        toks = self.tokens
        i = 0
        n = len(toks)
        while i < n - 2:
            if toks[i].kind == 'ident' and toks[i].value == 'function':
                j = i + 1
                if j < n and toks[j].kind == 'punct' and toks[j].value == '*':
                    j += 1
                if j >= n:
                    break
                name = ''
                if toks[j].kind == 'ident':
                    name = toks[j].value
                    j += 1
                if j < n and toks[j].kind == 'punct' and toks[j].value == '(':
                    close = _match_bracket(toks, j)
                    params, defaults = self._parse_params(j + 1, close)
                    self.param_ranges.append((j, close))
                    if close + 1 < n and toks[close + 1].value == '{':
                        body_end = _match_bracket(toks, close + 1)
                        self.functions.append(FunctionDef(
                            name=name or '(anonymous)', params=params,
                            defaults=defaults, body_start=close + 1,
                            body_end=body_end,
                            line=self.line_of_token(i), scope='file'))
                        i = close + 2
                        continue
            i += 1

    def _parse_params(self, start: int, end: int):
        params: list[str] = []
        defaults: dict[str, tuple[int, int]] = {}
        for (a, b) in _split_top(self.tokens, start, end, ','):
            if a >= b:
                continue
            name = self.tokens[a].value
            if self.tokens[a].kind == 'ident':
                params.append(name)
                if a + 1 < b and self.tokens[a + 1].value == '=':
                    defaults[name] = (a + 2, b)
        return params, defaults

    def _assign_scopes(self) -> None:
        """Give every function its own scope; nested functions get theirs."""
        spans: list[FunctionDef] = list(self.functions)
        for idx, fn in enumerate(self.functions):
            fn.scope = 'fn%d:%s' % (idx, fn.name)
            self.scopes[fn.scope] = Scope(fn.scope)
        # map token index -> innermost function span
        self._fn_spans = sorted(
            ((fn.body_start, fn.body_end, fn.scope) for fn in spans),
            key=lambda t: t[1] - t[0])
        # calls + assignments are collected per scope below

    def scope_at(self, pos: int) -> str:
        best = None
        best_len = 1 << 60
        for (a, b, scope) in self._fn_spans:
            if a <= pos <= b and (b - a) < best_len:
                best = scope
                best_len = b - a
        return best or 'file'

    def _collect_bindings(self) -> None:
        toks = self.tokens
        n = len(toks)
        param_ranges = sorted(self.param_ranges)
        i = 0
        while i < n:
            # skip function parameter lists (defaults look like assignments)
            for (pa, pb) in param_ranges:
                if pa < i < pb:
                    i = pb
                    break
            else:
                pass
            if i >= n:
                break
            tok = toks[i]
            if tok.kind != 'ident':
                i += 1
                continue
            # skip keywords that cannot be LHS names in an assignment
            if tok.value in ('if', 'while', 'for', 'foreach', 'return', 'case',
                             'function', 'property', 'class', 'else', 'do',
                             'switch', 'try', 'catch', 'throw', 'new', 'in',
                             'var', 'typeof', 'instanceof', 'incontextof'):
                i += 1
                continue
            # find a simple assignment: IDENT '=' (not '=='), allowing `var X =`
            j = i + 1
            name = tok.value
            if j < n and toks[j].kind == 'punct' and toks[j].value == '.':
                # dotted LHS: keep the last component (this.x / global.x / obj.x)
                k = j + 1
                while k + 1 < n and toks[k].kind == 'ident' \
                        and toks[k + 1].value == '.':
                    k += 2
                if k < n and toks[k].kind == 'ident':
                    name = toks[k].value
                    j = k + 1
            if j >= n:
                i += 1
                continue
            if not (toks[j].kind == 'punct' and toks[j].value == '='):
                i += 1
                continue
            # RHS extends to the next depth-0 ';'
            depth = 0
            k = j + 1
            while k < n:
                v = toks[k].value
                if toks[k].kind == 'punct':
                    if v in ('(', '[', '{'):
                        depth += 1
                    elif v in (')', ']', '}'):
                        if depth == 0:
                            break
                        depth -= 1
                    elif v == ';' and depth == 0:
                        break
                k += 1
            if k > j + 1:
                b = Binding(name=name, expr=(j + 1, k), pos=i,
                            line=self.line_of_token(i))
                rhs = (j + 1, k)
                stripped = _strip_parens(toks, rhs[0], rhs[1])
                if stripped[0] < stripped[1] and toks[stripped[0]].value == '[':
                    close = _match_bracket(toks, stripped[0])
                    if close == stripped[1] - 1:
                        b.array_items = _split_top(toks, stripped[0] + 1,
                                                   close, ',')
                elif stripped[0] < stripped[1] and \
                        toks[stripped[0]].value == '%' and \
                        stripped[0] + 1 < stripped[1] and \
                        toks[stripped[0] + 1].value == '[':
                    close = _match_bracket(toks, stripped[0] + 1)
                    if close == stripped[1] - 1:
                        b.dict_items = self._parse_dict(stripped[0] + 2, close)
                self.scopes[self.scope_at(i)].add_binding(b)
            i = k + 1

    def _parse_dict(self, start: int, end: int) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for (a, b) in _split_top(self.tokens, start, end, ','):
            if a >= b:
                continue
            key_tok = self.tokens[a]
            if key_tok.kind not in ('ident', 'str'):
                continue
            if a + 1 < b and self.tokens[a + 1].value == ':':
                out[key_tok.value] = (a + 2, b)
        return out

    # -- calls ---------------------------------------------------------
    def collect_calls(self) -> list[CallSite]:
        toks = self.tokens
        n = len(toks)
        out: list[CallSite] = []
        i = 0
        while i < n:
            if toks[i].kind != 'ident' or toks[i].value in (
                    'if', 'while', 'for', 'foreach', 'function', 'return',
                    'typeof', 'switch', 'catch', 'new', 'delete', 'in'):
                i += 1
                continue
            # dotted callee
            parts = [toks[i].value]
            j = i + 1
            while j + 1 < n and toks[j].value == '.' and toks[j + 1].kind == 'ident':
                parts.append(toks[j + 1].value)
                j += 2
            if j < n and toks[j].value == '(':
                close = _match_bracket(toks, j)
                args = _split_top(toks, j + 1, close, ',')
                out.append(CallSite(name='.'.join(parts), last=parts[-1],
                                    open_idx=j, close_idx=close, args=args,
                                    pos=i, line=self.line_of_token(i),
                                    scope=self.scope_at(i)))
                i = j + 1
                continue
            i += 1
        self.calls = out
        return out


def _strip_parens(tokens: Sequence[Token], start: int, end: int) -> tuple[int, int]:
    while start < end and tokens[start].kind == 'punct' \
            and tokens[start].value == '(':
        close = _match_bracket(tokens, start)
        if close != end - 1:
            break
        start += 1
        end -= 1
    return start, end


class Resolver:
    """Resolve token ranges to concrete string sets using scope bindings."""

    def __init__(self, source: SourceFile,
                 param_vals: dict[str, set[str]] | None = None,
                 extra_values: dict[str, set[str]] | None = None,
                 hide: frozenset[str] = frozenset(),
                 pos: int = 1 << 60) -> None:
        self.source = source
        self.tokens = source.tokens
        self.params = param_vals or {}
        self.extra = extra_values or {}
        self.hide = hide
        self.pos = pos
        self._cache: dict[tuple[int, int, str], set[str] | None] = {}

    # -- public --------------------------------------------------------
    def resolve(self, start: int, end: int, scope: str,
                depth: int = 0) -> set[str] | None:
        if start >= end:
            return None
        key = (start, end, scope)
        if key in self._cache:
            return self._cache[key]
        res = self._resolve(start, end, scope, depth)
        if res is not None:
            if len(res) > MAX_VALUES:
                res = set(sorted(res)[:MAX_VALUES])
        self._cache[key] = res
        return res

    # -- internals -----------------------------------------------------
    def _resolve(self, start: int, end: int, scope: str,
                 depth: int) -> set[str] | None:
        if depth > MAX_DEPTH:
            return None
        toks = self.tokens
        start, end = _strip_parens(toks, start, end)
        if start >= end:
            return None
        # single literal
        if end - start == 1:
            tok = toks[start]
            if tok.kind == 'str':
                return {tok.value}
            if tok.kind == 'ident':
                return self.lookup_ident(tok.value, scope)
            if tok.kind == 'num':
                return None
            return None
        # array literal -> union of elements
        if toks[start].value == '[':
            close = _match_bracket(toks, start)
            if close == end - 1:
                out: set[str] = set()
                for (a, b) in _split_top(toks, start + 1, close, ','):
                    part = self.resolve(a, b, scope, depth + 1)
                    if part:
                        out |= part
                return out or None
        # ternary
        q = self._find_top(start, end, '?')
        if q >= 0:
            colon = self._find_top(q + 1, end, ':')
            if colon >= 0:
                a = self.resolve(q + 1, colon, scope, depth + 1) or set()
                b = self.resolve(colon + 1, end, scope, depth + 1) or set()
                return (a | b) or None
        # concatenation
        parts = _split_top(toks, start, end, '+')
        if len(parts) > 1:
            out = {''}
            for (a, b) in parts:
                vals = self.resolve(a, b, scope, depth + 1)
                if vals is None:
                    return None
                out = {x + y for x in out for y in vals}
                if len(out) > MAX_VALUES:
                    out = set(sorted(out)[:MAX_VALUES])
            return out
        # function-style / method call
        ci = self._call_shape(start, end)
        if ci is not None:
            name, inner_start, inner_end = ci
            return self._resolve_call(name, inner_start, inner_end, scope, depth)
        # indexing into a literal array: arr[i]
        idx = self._index_shape(start, end)
        if idx is not None:
            base, i_start, i_end = idx
            arr = self._lookup_array(base, scope)
            if arr is not None:
                ivals = self.resolve(i_start, i_end, scope, depth + 1)
                if ivals and len(ivals) == 1:
                    try:
                        pos = int(list(ivals)[0])
                    except ValueError:
                        pos = None
                    if pos is not None and 0 <= pos < len(arr):
                        return self.resolve(arr[pos][0], arr[pos][1],
                                            scope, depth + 1)
                out = set()
                for (a, b) in arr:
                    part = self.resolve(a, b, scope, depth + 1)
                    if part:
                        out |= part
                return out or None
        return None

    def _find_top(self, start: int, end: int, op: str) -> int:
        depth = 0
        for j in range(start, end):
            tok = self.tokens[j]
            if tok.kind != 'punct':
                continue
            if tok.value in ('(', '[', '{'):
                depth += 1
            elif tok.value in (')', ']', '}'):
                depth -= 1
            elif depth == 0 and tok.value == op:
                return j
        return -1

    def _call_shape(self, start: int, end: int):
        toks = self.tokens
        # NAME '(' ... ')'  |  EXPR '.' NAME '(' ... ')'
        if toks[end - 1].value != ')':
            return None
        depth = 0
        open_idx = -1
        for j in range(end - 1, start - 1, -1):
            v = toks[j].value
            if toks[j].kind != 'punct':
                continue
            if v == ')':
                depth += 1
            elif v == '(':
                depth -= 1
                if depth == 0:
                    open_idx = j
                    break
        if open_idx <= start:
            return None
        # callee text between start..open_idx
        if toks[open_idx - 1].kind != 'ident':
            return None
        name = toks[open_idx - 1].value
        k = open_idx - 2
        while k >= start and toks[k].value == '.' and k >= start + 1 \
                and toks[k - 1].kind == 'ident':
            name = toks[k - 1].value + '.' + name
            k -= 2
        if k >= start:
            return None  # receiver is an expression; not a plain value call
        return name, open_idx + 1, end - 1

    def _index_shape(self, start: int, end: int):
        toks = self.tokens
        if toks[end - 1].value != ']':
            return None
        if start >= end or toks[start].kind != 'ident':
            return None
        if start + 1 >= end or toks[start + 1].value != '[':
            return None
        close = _match_bracket(toks, start + 1)
        if close != end - 1:
            return None
        return toks[start].value, start + 2, close

    def _resolve_call(self, name: str, start: int, end: int, scope: str,
                      depth: int) -> set[str] | None:
        last = name.rsplit('.', 1)[-1]
        args = _split_top(self.tokens, start, end, ',')
        if last in ('string', 'String', 'toString'):
            if args:
                return self.resolve(args[0][0], args[0][1], scope, depth + 1)
            return None
        if last in ('chopStorageExt', 'extractStorageName'):
            if args:
                vals = self.resolve(args[0][0], args[0][1], scope, depth + 1)
                if vals is None:
                    return None
                return {_chop_ext(v) for v in vals}
            return None
        if last == 'sprintf':
            # expr.sprintf(args...) -- the format is the receiver expression
            return self._sprintf(name, start, end, scope, depth)
        return None

    def _sprintf(self, name: str, start: int, end: int, scope: str,
                 depth: int) -> set[str] | None:
        toks = self.tokens
        # locate '.sprintf' and the receiver range start..dot
        dot = -1
        open_idx = -1
        depth2 = 0
        for j in range(end - 1, start - 1, -1):
            v = toks[j].value
            if toks[j].kind != 'punct':
                continue
            if v == ')':
                depth2 += 1
            elif v == '(':
                depth2 -= 1
                if depth2 == 0:
                    open_idx = j
                    break
        if open_idx < 0:
            return None
        if open_idx - 1 >= start and toks[open_idx - 1].value == 'sprintf':
            dot = open_idx - 2
        if dot < start:
            return None
        fmts = self.resolve(start, dot, scope, depth + 1)
        if not fmts:
            return None
        args = _split_top(toks, open_idx + 1, end - 1, ',')
        argvals: list[set[str] | None] = []
        for (a, b) in args:
            argvals.append(self.resolve(a, b, scope, depth + 1))
        out: set[str] = set()
        for fmt in fmts:
            outs = {fmt}
            pos = 0
            ok = True
            for vals in argvals:
                m = re.search(r'%(?:[-+ 0#]*\d*(?:\.\d+)?)([sdifxX%])', fmt[pos:])
                if not m:
                    break
                conv = m.group(1)
                spec = m.group(0)
                if conv == '%':
                    continue
                if vals is None:
                    ok = False
                    break
                if conv in 'di':
                    nums = set()
                    for v in vals:
                        if v.lstrip('-').isdigit():
                            nums.add(v)
                    if not nums:
                        ok = False
                        break
                    vals = nums
                nxt: set[str] = set()
                for prefix in outs:
                    for v in vals:
                        nxt.add(prefix + v)
                outs = nxt
                pos += m.end()
                if len(outs) > MAX_VALUES:
                    ok = False
                    break
            if not ok:
                continue
            out |= outs
        return out or None

    def lookup_ident(self, name: str, scope: str) -> set[str] | None:
        if name in self.hide:
            return None
        if name in self.params:
            return self.params[name]
        if name in self.extra:
            return self.extra[name]
        s = self.source.scopes.get(scope)
        if s is not None and name in s.bindings:
            # the decompiler reuses localN names in every function, so a
            # function-local that does not resolve must NOT fall through to a
            # same-named file-scope temp.
            return s.lookup_at(name, self.pos)
        if s is not None:
            vals = s.lookup_at(name, self.pos)
            if vals:
                return vals
        if name in self.source.file_scope.bindings:
            return self.source.file_scope.lookup_at(name, self.pos)
        # a file-local wrapper function used as a value: ``local0 = addArchive;``
        if name in WRAPPER_FUNCS and any(f.name == name
                                         for f in self.source.functions):
            return {name}
        return None

    def _lookup_array(self, name: str, scope: str):
        def _pick(entries):
            if not entries:
                return None
            before = [e for e in entries if e[0] < self.pos]
            return (max(before, key=lambda e: e[0]) if before else entries[0])[1]

        s = self.source.scopes.get(scope)
        if s is not None and name in s.arrays:
            return _pick(s.arrays[name])
        if s is not None and name in s.bindings:
            return None
        if name in self.source.file_scope.arrays:
            return _pick(self.source.file_scope.arrays[name])
        return None


def _chop_ext(value: str) -> str:
    slash = max(value.rfind('/'), value.rfind('\\'))
    dot = value.rfind('.')
    if dot > slash + 1:
        return value[:dot]
    return value


def _refs_name(sf: SourceFile, expr: tuple[int, int], name: str) -> bool:
    """Does the token range reference ``name`` as a bare identifier?"""
    for j in range(expr[0], expr[1]):
        tok = sf.tokens[j]
        if tok.kind == 'ident' and tok.value == name:
            return True
    return False


# ===========================================================================
# Source analysis
# ===========================================================================
class Template:
    """A partially resolved argument expression (literal parts + placeholders)."""

    __slots__ = ('text',)

    def __init__(self, text: str) -> None:
        self.text = text

    def instantiate(self, argvals: dict[int, set[str]]) -> set[str]:
        out = {self.text}
        for m in _PLACEHOLDER_RE.finditer(self.text):
            idx = int(m.group(1))
            vals = argvals.get(idx)
            if not vals:
                return set()
            nxt: set[str] = set()
            for prefix in out:
                nxt.add(prefix)
            out = {prefix.replace(m.group(0), v)
                   for prefix in out for v in vals}
            if len(out) > MAX_VALUES:
                out = set(sorted(out)[:MAX_VALUES])
        return out


@dataclass
class _ApiHit:
    api: ApiRule
    value: str
    source_key: str
    line: int
    snippet: str
    via: str = ''


class Analyzer:
    """Whole-corpus static analyser."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.sources = sources
        self.files: dict[str, SourceFile] = {}
        # function name -> list of (source_key, ApiRule, Template)
        self.templates: dict[str, list[tuple[str, ApiRule, Template]]] = {}
        # phase-2 special enumerations (collected in build())
        self._transition_names: set[str] = set()
        self._has_trans_prefix = False
        self._storage_stems: list[tuple[str, int, str, tuple[str, ...]]] = []
        self._lang_defaults: list[tuple[str, int, str, str]] = []
        self._help_manager_files: list[tuple[str, int, str]] = []
        self._pagemap_pbds: list[tuple[str, int, str]] = []
        self._extra_hits: list[_ApiHit] = []
        self.stats: dict[str, Any] = {
            'sources': len(sources),
            'call_sites': 0,
            'api_calls': {},
            'api_resolved': {},
            'facts': {KIND_NAME: 0, KIND_DIR: 0, KIND_FRAGMENT: 0},
            'unresolved_samples': [],
            'template_calls': 0,
        }

    # -- setup ---------------------------------------------------------
    def build(self) -> None:
        for key, src in self.sources.items():
            try:
                sf = SourceFile(key, src)
            except Exception:
                continue
            self.files[key] = sf
            sf.collect_calls()
        # pass 1: resolve constant bindings (several rounds for forward refs)
        for _round in range(4):
            for sf in self.files.values():
                self._resolve_scope(sf.file_scope, sf)
                for scope in sf.scopes.values():
                    if scope.ident == 'file':
                        continue
                    self._resolve_scope(scope, sf)
        # pass 2: extract API-call templates of wrapper functions
        for sf in self.files.values():
            self._extract_templates(sf)
        # pass 3: phase-2 special enumerations (language / thumbnails / tabs)
        for sf in self.files.values():
            self._collect_special(sf)

    def _resolve_scope(self, scope: Scope, sf: SourceFile) -> None:
        if not scope.bindings:
            return
        for name, bindings in scope.bindings.items():
            plain = [b for b in bindings if not _refs_name(sf, b.expr, name)]
            mutated = [b for b in bindings if _refs_name(sf, b.expr, name)]
            hidden = Resolver(sf, hide=frozenset({name}))
            base: set[str] = set()
            for b in plain:
                got = hidden.resolve(b.expr[0], b.expr[1], scope.ident)
                if got:
                    base |= got
            if len(base) > MAX_VALUES:
                base = set(sorted(base)[:MAX_VALUES])
            entries: list[tuple[int, set[str]]] = []
            for b in plain:
                got = hidden.resolve(b.expr[0], b.expr[1], scope.ident)
                if got:
                    entries.append((b.pos, set(got)))
            if mutated and base:
                # values = ... (self mutation, e.g. ``x = x + "_allage"``):
                # resolve against the *base* values only, never against the
                # previous round's result, otherwise the string keeps growing.
                mres = Resolver(sf, extra_values={name: base})
                for b in mutated:
                    got = mres.resolve(b.expr[0], b.expr[1], scope.ident)
                    if got:
                        entries.append((b.pos, set(got)))
            if not entries:
                continue
            entries.sort(key=lambda e: e[0])
            scope.binding_values[name] = entries
            allvals: set[str] = set()
            for _pos, vals in entries:
                allvals |= vals
            if len(allvals) > MAX_VALUES:
                allvals = set(sorted(allvals)[:MAX_VALUES])
            scope.values[name] = allvals

    def _extract_templates(self, sf: SourceFile) -> None:
        for fn in sf.functions:
            if fn.name not in WRAPPER_FUNCS:
                continue
            entries: list[tuple[str, ApiRule, Template]] = []
            for call in sf.calls:
                if not (fn.body_start < call.pos < fn.body_end):
                    continue
                rules = _RULE_BY_NAME.get(call.last)
                if not rules:
                    continue
                rule = rules[0]
                if rule.arg >= len(call.args):
                    continue
                a, b = call.args[rule.arg]
                tmpl = self._template_of(sf, a, b, call.scope, fn.params,
                                         pos=call.pos)
                if tmpl is not None and tmpl.text:
                    entries.append((sf.key, rule, tmpl))
            if entries:
                self.templates.setdefault(fn.name, []).extend(entries)

    def _template_of(self, sf: SourceFile, start: int, end: int, scope: str,
                     params: Sequence[str], depth: int = 0,
                     pos: int = 1 << 60) -> Template | None:
        """Resolve an expression keeping function parameters as placeholders."""
        if depth > 4:
            return None
        toks = sf.tokens
        start, end = _strip_parens(toks, start, end)
        if start >= end:
            return None
        resolver = Resolver(sf, pos=pos)
        # bare parameter reference
        if end - start == 1 and toks[start].kind == 'ident':
            name = toks[start].value
            if name in params:
                return Template(PLACEHOLDER % list(params).index(name))
            vals = resolver.resolve(start, end, scope)
            if vals and len(vals) == 1:
                return Template(list(vals)[0])
            return None
        # concatenation
        ranges = _split_top(toks, start, end, '+')
        if len(ranges) > 1:
            parts: list[str] = []
            for (a, b) in ranges:
                sub = self._template_of(sf, a, b, scope, params, depth + 1, pos)
                if sub is None:
                    return None
                parts.append(sub.text)
            return Template(''.join(parts))
        # string(...) / String(...) wrapper
        shape = resolver._call_shape(start, end)
        if shape and shape[0] in ('string', 'String'):
            inner = _split_top(toks, shape[1], shape[2], ',')
            if inner:
                return self._template_of(sf, inner[0][0], inner[0][1],
                                         scope, params, depth + 1, pos)
        # a fully constant sub-expression
        vals = resolver.resolve(start, end, scope)
        if vals and len(vals) == 1:
            return Template(list(vals)[0])
        return None

    # -- main ----------------------------------------------------------
    def analyze(self) -> Iterator[Fact]:
        hits: list[_ApiHit] = []
        for sf in self.files.values():
            for call in sf.calls:
                self.stats['call_sites'] += 1
                resolver = Resolver(sf, pos=call.pos)
                rules = _RULE_BY_NAME.get(call.last)
                if rules:
                    for rule in rules:
                        self.stats['api_calls'].setdefault(call.last, 0)
                        self.stats['api_calls'][call.last] += 1
                        got = self._resolve_call_arg(sf, resolver, call, rule)
                        for value in got:
                            hits.append(_ApiHit(rule, value, sf.key, call.line,
                                                sf.snippet(call.line),
                                                via=call.name))
                # KAG command dictionaries: %[storage: ...]
                hits.extend(self._dict_storage_hits(sf, resolver, call))
                # wrapper-function calls: instantiate recorded templates
                names = [call.last]
                if call.last not in self.templates and '.' not in call.name:
                    alias = resolver.lookup_ident(call.last, call.scope)
                    if alias:
                        names.extend(v for v in alias
                                     if v in self.templates and v != call.last)
                for target in names:
                    if target in self.templates:
                        hits.extend(self._instantiate_templates(
                            sf, resolver, call, target))
        # phase-2 special enumerations (language tags, transitions, thumbnails,
        # UI page names) gathered from the whole corpus in build()
        hits.extend(self._special_hits())
        # emit facts
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            fact = self._hit_to_fact(hit)
            if fact is None:
                continue
            key = (fact.kind, fact.value)
            if key in seen:
                continue
            seen.add(key)
            self.stats['facts'][fact.kind] = self.stats['facts'].get(fact.kind, 0) + 1
            self.stats['api_resolved'][hit.api.name] = \
                self.stats['api_resolved'].get(hit.api.name, 0) + 1
            yield fact

    def _resolve_call_arg(self, sf: SourceFile, resolver: Resolver,
                          call: CallSite, rule: ApiRule) -> set[str]:
        if rule.arg >= len(call.args):
            if rule.kind == 'dir':
                return set()
            return set()
        a, b = call.args[rule.arg]
        # skip named/weird args (e.g. "%s.sprintf(...)" handled elsewhere)
        vals = resolver.resolve(a, b, call.scope)
        if not vals:
            if len(self.stats['unresolved_samples']) < 200:
                self.stats['unresolved_samples'].append({
                    'api': call.last, 'source': sf.key, 'line': call.line,
                    'snippet': sf.snippet(call.line)})
            return set()
        out: set[str] = set()
        for v in vals:
            v = self._apply_api_suffix(rule, v)
            if v:
                out.add(v)
        return out

    @staticmethod
    def _apply_api_suffix(rule: ApiRule, value: str) -> str | None:
        value = value.strip()
        if not _plausible_storage(value):
            return None
        if rule.kind == 'dir':
            if '://' in value or value.startswith('%') or '\\' in value:
                return None       # proxy://, System.exePath + ... , win paths
            if '.exePath' in value or '>' in value:
                return None
            if value.startswith('/'):
                return None
            body = value[:-1] if value.endswith('/') else value
            if not body or body.startswith('.') or '..' in body:
                return None
            if '/' in body and any(part in ('', '.', '..')
                                   for part in body.split('/')):
                return None
            if _tjs_has_ext(body):
                return None       # a file name, not a directory
            if not value.endswith('/'):
                value += '/'
            return value
        if rule.kind == 'plugin':
            if not value.lower().endswith('.dll'):
                value += rule.suffix
            return value
        if rule.kind == 'script':
            if _tjs_has_ext(value):
                return value
            return value + rule.suffix
        return value

    def _dict_storage_hits(self, sf: SourceFile, resolver: Resolver,
                           call: CallSite) -> list[_ApiHit]:
        out: list[_ApiHit] = []
        for (a, b) in call.args:
            a2, b2 = _strip_parens(sf.tokens, a, b)
            if not (a2 + 1 < b2 and sf.tokens[a2].value == '%'
                    and sf.tokens[a2 + 1].value == '['):
                continue
            close = _match_bracket(sf.tokens, a2 + 1)
            entries = sf._parse_dict(a2 + 2, close)
            for key, (ea, eb) in entries.items():
                if key not in DICT_STORAGE_KEYS:
                    continue
                vals = resolver.resolve(ea, eb, call.scope)
                if not vals:
                    continue
                if key in _DICT_IMAGE_KEYS:
                    context = 'tjs:image.storage'
                elif key in _DICT_AUDIO_KEYS:
                    context = 'tjs:audio.storage'
                else:
                    context = 'tjs:storage'
                for v in vals:
                    rule = ApiRule(name=key, arg=0, kind='storage',
                                   context=context,
                                   note='KAG command dict %r' % key)
                    out.append(_ApiHit(rule, v, sf.key, call.line,
                                       sf.snippet(call.line),
                                       via=call.name))
        return out

    def _instantiate_templates(self, sf: SourceFile, resolver: Resolver,
                               call: CallSite, target: str) -> list[_ApiHit]:
        out: list[_ApiHit] = []
        entries = self.templates.get(target) or []
        if not entries:
            return out
        # resolve arguments once
        argvals: dict[int, set[str]] = {}
        for i, (a, b) in enumerate(call.args):
            vals = resolver.resolve(a, b, call.scope)
            if vals:
                argvals[i] = vals
        if not argvals:
            return out
        n = 0
        for (_key, rule, tmpl) in entries:
            vals = tmpl.instantiate(argvals)
            for v in vals:
                v = self._apply_api_suffix(rule, v)
                if not v:
                    continue
                out.append(_ApiHit(rule, v, sf.key, call.line,
                                   sf.snippet(call.line), via=call.name))
                n += 1
                if n > MAX_TEMPLATE_INSTANCES:
                    return out
        if out:
            self.stats['template_calls'] += 1
        return out

    # -- phase 2: corpus-wide special enumerations ---------------------
    def _arg_values(self, sf: SourceFile, resolver: Resolver, call: CallSite,
                    idx: int = 0) -> list[str]:
        if idx >= len(call.args):
            return []
        a, b = call.args[idx]
        vals = resolver.resolve(a, b, call.scope)
        return sorted(vals) if vals else []

    def _mk(self, sf: SourceFile, call: CallSite | None, value: str,
            context: str, note: str, line: int = 0) -> _ApiHit:
        rule = ApiRule(name=context, arg=0, kind='storage', context=context,
                       note=note)
        if call is not None:
            return _ApiHit(rule, value, sf.key, call.line,
                           sf.snippet(call.line), via=call.name)
        return _ApiHit(rule, value, sf.key, line, sf.snippet(line), via='')

    @staticmethod
    def _lang_variants(value: str) -> list[str]:
        """``name.ext`` -> [name.ext, name_en.ext, name_cn.ext, name_tw.ext]."""
        value = value.strip()
        if not value or '.' not in value.rsplit('/', 1)[-1]:
            return [value]
        stem, dot, ext = value.rpartition('.')
        if '.' in stem.rsplit('/', 1)[-1]:
            return [value]
        out = [value]
        for lang in NONJP_LANGS:
            out.append('%s_%s.%s' % (stem, lang, ext))
        return out

    def _collect_special(self, sf: SourceFile) -> None:
        src = sf.src
        # a) transition names table (world.tjs: this.transitionName = %[...])
        #    + the config resolver getParameter(x, "transitions", "trans_")
        #    which loads "trans_" + x + ".tjs" (world.tjs lines 7525-7541).
        if '"transitions"' in src and '"trans_"' in src:
            self._has_trans_prefix = True
        for scope in sf.scopes.values():
            for bindings in scope.bindings.values():
                for b in bindings:
                    if b.dict_items and b.name == 'transitionName':
                        for key in b.dict_items:
                            if key and not key.startswith('-'):
                                self._transition_names.add(key)
        # b) storage / glyph literals whose file name gets a proven extension
        #    *xxxStorage = "stem" -> UI storage stem (PBD / image)
        #    *Glyph = "stem"      -> glyph image + its affinesource .asd
        for m in re.finditer(
                r'([A-Za-z_]\w*(?:Storage|Glyph))\s*[=:]\s*"([^"\r\n]{2,80})"',
                src):
            key, val = m.group(1), m.group(2)
            line = sf.line_of(m.start())
            if key.endswith('Glyph') or val.startswith(GLYPH_PREFIXES):
                exts: tuple[str, ...] = ('.png', '.tlg', '.asd')
            else:
                exts = ('.pbd',)
                if val.startswith(('autoskipmark', 'linebreak', 'pagebreak')):
                    exts = ('.pbd', '.png', '.asd')
            self._storage_stems.append((sf.key, line, val, exts))
        # c) option page storages (uiqconfpop.tjs: optionStorage = "qconf_...")
        for m in re.finditer(
                r'(?:optionStorage|uiStorage|uistorage)\s*=\s*"([^"\r\n]{2,80})"',
                src):
            self._storage_stems.append(
                (sf.key, sf.line_of(m.start()), m.group(1), ('.pbd',)))
        # d) HelpTextManager(..., "help_xxx.txt") files -> language variants
        for m in re.finditer(
                r'new\s+HelpTextManager\([^)]*?"([^"\r\n]+\.txt)"', src):
            self._help_manager_files.append(
                (sf.key, sf.line_of(m.start()), m.group(1)))
        # d2) any localizable data-file literal in a source that resolves files
        #     through GetSystemLangFile()/getHelpTextFile(): those functions are
        #     the language-suffix mechanism, so their literal inputs get
        #     "<stem>_<lang><ext>" variants.  (speech_dlg.tjs keeps e.g.
        #     "speech_help.txt" in a local before calling getHelpTextFile().)
        if any(h in src for h in ('GetSystemLangFile', 'getHelpTextFile',
                                  'HelpTextManager', 'loadTranslateTable')):
            for m in re.finditer(
                    r'"([A-Za-z0-9_./-]{2,120}\.(?:txt|ini|toml))"', src):
                val = m.group(1)
                if val.startswith(('http', '/')):
                    continue
                if not val.startswith(('help_', 'speech_', 'snapcapture_',
                                       'uitexts', 'flow_texts', 'syslangtext',
                                       'musictitle', 'lets')):
                    continue
                self._help_manager_files.append(
                    (sf.key, sf.line_of(m.start()), val))
        # d3) musictitle_<lang>.txt is built from the LanguageTags loop
        if 'musictitle_%s.txt' in src:
            for tag in LANGUAGE_TAGS:
                self._help_manager_files.append(
                    (sf.key, sf.line_of(src.find('musictitle_%s.txt')),
                     'musictitle_%s.txt' % tag))
        # d4) "qconf" UI namespace also has a base qconf.pbd (uimain.tjs
        #     arg1("qconf", ...) + uiqconfpop.tjs "qconf." + tag)
        if '"qconf."' in src or '"qconf_q' in src:
            self._storage_stems.append((sf.key, sf.line_of(0), 'qconf', ('.pbd',)))
        # e) title UI parts (uimain.tjs property storage) -> .func sidecars
        idx = src.find('property storage')
        if idx >= 0 and 'title' in src[idx:idx + 3000]:
            block = src[idx:idx + 3000]
            stems = set(re.findall(r'\.add\("([^"]+)"\)', block))
            for m in re.finditer(r'\["([^"]+)"', block):
                stems.add(m.group(1))
            if '"title_lang_"' in block:
                stems.update({'title_lang_left', 'title_lang_right'})
            for stem in sorted(stems):
                if not stem or '.' in stem or not stem.startswith('title'):
                    continue
                rule = ApiRule(
                    name='title_func', arg=0, kind='storage',
                    context='tjs:ui.func',
                    note='UI title storage part; ".func" sidecar '
                         '(uisystem.tjs _doInclImpt(..., ".func"))')
                self._extra_hits.append(_ApiHit(
                    rule, stem + '.func', sf.key, sf.line_of(idx),
                    sf.snippet(sf.line_of(idx)), via='property storage'))
        # f) LoadSysLangText default arguments: syslangtext_ + <tag> + .ini
        for fn in sf.functions:
            if fn.name != 'LoadSysLangText':
                continue
            pre = ext = ''
            for pname, (a, b) in fn.defaults.items():
                toks = sf.tokens[a:b]
                if len(toks) == 1 and toks[0].kind == 'str':
                    if pname in ('arg0', 'a') and pname == 'arg0':
                        pre = toks[0].value
                    elif pname == 'arg1':
                        ext = toks[0].value
            if pre and ext:
                self._lang_defaults.append((sf.key, fn.line, pre, ext))
        # g) option page map (uioption.tjs pagemaps) -> option_<page>.pbd
        if 'pagemaps' in src and '.pbd' in src:
            for m in re.finditer(r'pagemaps\s*=\s*%\[(.*?)\n\s*\]', src, re.S):
                for k in re.findall(r'"([^"]+)"\s*=>', m.group(1)):
                    for part in (k,):
                        self._pagemap_pbds.append(
                            (sf.key, sf.line_of(m.start()), 'option_' + part))

    def _special_hits(self) -> list[_ApiHit]:
        out: list[_ApiHit] = []
        for sf in self.files.values():
            for call in sf.calls:
                if not call.args:
                    continue
                if call.last in LANG_FILE_HELPERS:
                    resolver = Resolver(sf, pos=call.pos)
                    for v in self._arg_values(sf, resolver, call, 0):
                        for cand in self._lang_variants(v):
                            out.append(self._mk(
                                sf, call, cand, 'tjs:lang.file',
                                'GetSystemLangFile(): name_<lang>.ext'))
                elif call.last in LANG_IMAGE_HELPERS:
                    resolver = Resolver(sf, pos=call.pos)
                    for v in self._arg_values(sf, resolver, call, 0):
                        for lang in NONJP_LANGS:
                            for ext in IMAGE_EXT_NAMES:
                                out.append(self._mk(
                                    sf, call, '%s_%s%s' % (v, lang, ext),
                                    'tjs:ui.image',
                                    'GetUIImage(base, langTag) -> '
                                    'base_<lang> + image ext'))
                elif call.last in LANG_FORCE_HELPERS and len(call.args) >= 2:
                    resolver = Resolver(sf, pos=call.pos)
                    bases = self._arg_values(sf, resolver, call, 0)
                    exts = self._arg_values(sf, resolver, call, 1)
                    for base in bases:
                        for ext in exts:
                            for lang in NONJP_LANGS:
                                out.append(self._mk(
                                    sf, call, '%s_%s%s' % (base, lang, ext),
                                    'tjs:lang.file',
                                    'uilangforce(base, ext) -> '
                                    'base_<lang>ext'))
                elif call.last in K2COMPAT_DELAY:
                    resolver = Resolver(sf, pos=call.pos)
                    for v in self._arg_values(sf, resolver, call, 0):
                        out.append(self._mk(
                            sf, call, 'k2compat_%s.tjs' % v,
                            'tjs:k2compat',
                            'delayLoad*("name") -> require(name) -> '
                            'k2compat_<name>.tjs'))
                elif call.last == 'require' or call.name.endswith(
                        '.require'):
                    resolver = Resolver(sf, pos=call.pos)
                    for v in self._arg_values(sf, resolver, call, 0):
                        out.append(self._mk(
                            sf, call, 'k2compat_%s.tjs' % v, 'tjs:k2compat',
                            'Krkr2CompatUtils.require("name") -> '
                            'k2compat_<name>.tjs'))
        # transition scripts: trans_<name>.tjs for every transitionName key
        if self._has_trans_prefix:
            for name in sorted(self._transition_names):
                if not name or name.startswith('-'):
                    continue
                rule = ApiRule(name='transition_script', arg=0,
                               kind='storage', context='tjs:transition',
                               note='getParameter(name, "transitions", "trans_") '
                                    '-> trans_<name>.tjs')
                self._extra_hits.append(_ApiHit(
                    rule, 'trans_%s.tjs' % name, 'world.tjs', 0,
                    'this.transitionName = %%[...]', via='getParameter'))
        # storage / glyph stems with a proven file extension
        for key, line, val, exts in self._storage_stems:
            for ext in exts:
                rule = ApiRule(name='storage_stem', arg=0, kind='storage',
                               context='tjs:storage.stem',
                               note='storage/glyph literal + proven sidecar ext')
                out.append(_ApiHit(rule, val + ext, key, line, '', via=''))
        # HelpTextManager help_*.txt -> language variants
        for key, line, val in self._help_manager_files:
            for cand in self._lang_variants(val):
                rule = ApiRule(name='help_lang', arg=0, kind='storage',
                               context='tjs:lang.file',
                               note='HelpTextManager help file; '
                                    'getHelpTextFile() adds _<lang>')
                out.append(_ApiHit(rule, cand, key, line, '', via=''))
        # LoadSysLangText default args -> syslangtext_<tag>.ini
        for key, line, pre, ext in self._lang_defaults:
            for tag in LANGUAGE_TAGS:
                rule = ApiRule(name='syslangtext', arg=0, kind='storage',
                               context='tjs:lang.file',
                               note='LoadSysLangText loop over LanguageTags')
                out.append(_ApiHit(rule, '%s%s%s' % (pre, tag, ext),
                                   key, line, '', via='LoadSysLangText'))
        # option page PBDs
        for key, line, stem in self._pagemap_pbds:
            rule = ApiRule(name='option_pbd', arg=0, kind='storage',
                           context='tjs:ui.pbd',
                           note='uioption.tjs pagemaps -> option_<page>.pbd')
            out.append(_ApiHit(rule, stem + '.pbd', key, line, '', via=''))
        out.extend(self._extra_hits)
        return out

    def _hit_to_fact(self, hit: _ApiHit) -> Fact | None:
        value = hit.value
        if not _plausible_storage(value):
            return None
        kind: str
        note = hit.api.note
        if value.endswith('/'):
            kind = KIND_DIR
            context = hit.api.context
        elif _tjs_has_ext(value):
            kind = KIND_NAME
            context = hit.api.context
        else:
            kind = KIND_FRAGMENT
            context = hit.api.context
        source = 'logic:%s:%d' % (hit.source_key, hit.line)
        return Fact(value=value, kind=kind, source=source,
                    note='%s: %s' % (note, hit.snippet[:140]),
                    context=context)


def _tjs_has_ext(value: str) -> bool:
    exts = getattr(_tjs, 'KNOWN_EXTS', ())
    lo = value.lower()
    for ext in exts:
        if lo.endswith(ext):
            return True
    return False


# Windows-invalid characters + characters that cannot be part of a storage
# name.  ``?``/``*`` are excluded too; TJS code fragments use them heavily.
_STORAGE_BAD = set('\\:*?"<>|\r\n\t\x00')
_TEMPLATE_MARKS = ('${', '%s', '%d', '%02d', '%04d', '%08x', '//', '://')


def _plausible_storage(value: str) -> bool:
    """Cheap sanity filter for a resolved storage-name candidate."""
    if not value or len(value) > 200:
        return False
    if value != value.strip():
        return False
    if value.startswith('.'):
        return False
    for ch in value:
        if ch in _STORAGE_BAD:
            return False
        if ord(ch) < 0x20:
            return False
    if any(mark in value for mark in _TEMPLATE_MARKS):
        return False
    if any(ch in value for ch in '(){};=[]'):
        return False
    if value.startswith('^') or value.startswith('*') or value.startswith('?'):
        return False
    return True


def _strip_autopath(value: str) -> str | None:
    lo = value.lower()
    for prefix in AUTOPATH_PREFIXES:
        if lo.startswith(prefix):
            rest = value[len(prefix):]
            if rest and '/' not in rest:
                return rest
    return None


def _postprocess(facts: Iterable[Fact]) -> Iterator[Fact]:
    """Add variants that the game's own storage layer proves are equivalent.

    1. **lower-case variant** -- the XP3 entries of this family are stored with
       fully lower-case names (the tenshi assets are all lower-case, including
       the ones the scripts load as ``KAGLoadScript("AffineMatrix.tjs")``).  The
       decompiled sources consistently use CamelCase, so both spellings are emitted.
    2. **archive-root variant** -- ``Scripts.execStorage("system/Initialize.tjs")``
       is stored as ``initialize.tjs`` because ``initialize.tjs`` mounts
       ``system/`` as an auto-path (see ``initialize.tjs`` data#1223).
    """
    for fact in facts:
        yield fact
        if fact.kind == KIND_DIR:
            low = fact.value.lower()
            if low != fact.value:
                yield Fact(value=low, kind=KIND_DIR, source=fact.source,
                           note='lower-case variant: %s' % fact.note,
                           context=fact.context + '.lower')
            continue
        if fact.kind != KIND_NAME:
            continue
        alts: list[tuple[str, str]] = []
        low = fact.value.lower()
        if low != fact.value:
            alts.append((low, 'lower-case variant'))
        stripped = _strip_autopath(fact.value)
        if stripped and stripped != fact.value:
            alts.append((stripped, 'autopath-stripped variant'))
            low_stripped = stripped.lower()
            if low_stripped != stripped:
                alts.append((low_stripped, 'autopath-stripped, lower-case'))
        for value, why in alts:
            yield Fact(value=value, kind=KIND_NAME, source=fact.source,
                       note='%s: %s' % (why, fact.note),
                       context=fact.context + '.' + why.split(',')[0]
                       .replace(' ', '-'))


# ===========================================================================
# Public API
# ===========================================================================
_LAST_REPORT: dict[str, Any] = {}


def last_report() -> dict[str, Any]:
    """Statistics of the last :func:`analyze_sources` call."""
    return dict(_LAST_REPORT)


def analyze_sources(sources: dict[str, str]) -> Iterator[Fact]:
    """Analyse decompiled TJS sources, yielding file-name Facts.

    Parameters
    ----------
    sources
        ``{key: decompiled TJS2 source}``.  The key is only used for
        provenance (``logic:<key>:<line>``); e.g. ``'data#1230'``.
    """
    analyzer = Analyzer(sources)
    analyzer.build()
    facts = list(analyzer.analyze())
    analyzer.stats['facts_total'] = len(facts)
    _LAST_REPORT.clear()
    _LAST_REPORT.update(analyzer.stats)
    return iter(list(_postprocess(facts)))


def iter_facts(entry, data: bytes) -> Iterator[Fact]:
    """Single-entry convenience wrapper: decompile TJS2100 then analyse it."""
    try:
        kind = _tjs.sniff(data)
    except Exception:
        kind = None
    if kind != 'tjs':
        return iter(())
    try:
        source = _tjs.decompile(data)
    except Exception:
        return iter(())
    key = '%s#%s' % (getattr(entry, 'archive', '?'),
                     getattr(entry, 'index', '?'))
    return analyze_sources({key: source})


# ===========================================================================
# Evidence-backed rules for rules.py
# ===========================================================================
def engine_rules() -> list[dict]:
    """Naming conventions / extension lists proven by the decompiled sources.

    Every rule carries the concrete evidence (script file + line + code) so the
    provenance shown to the user can be traced back.  ``detail`` is structured
    for ``rules.py``; ``evidence`` is human readable with the script name and
    its ``<archive>#<entry index>`` corpus key.
    """
    return [
        {
            'id': 'tjs.image.extensions',
            'detail': {
                'kind': 'ext-list',
                'contexts': ['tjs:image.storage', 'pbd:layerFilename',
                             'pbd:storage', 'pbd:holdimage', 'tjs:ui.image'],
                'exts': ['.png', '.jpg', '.tlg', '.pimg', '.tlg5', '.tlg6',
                         '.bmp', '.psd'],
                'preferred': '.tlg',
            },
            'evidence': ('utils.tjs (data#1281) line 70: '
                         'global.Layer.SupportedExtensions = ["png", "jpg", '
                         '"tlg", "pimg", "tlg5", "tlg6", "bmp", "psd"];  -- '
                         'getExistImageName() (line 507) feeds this list to '
                         'getExistFileNameAutoExtFill(), i.e. a storage stem '
                         'expands to <stem>.<ext> in that order.'),
        },
        {
            'id': 'tjs.image.auto-ext-fill',
            'detail': {
                'kind': 'ext-fill',
                'function': 'getExistFileNameAutoExtFill',
                'detail': ('if name has "." or does not exist, chopStorageExt('
                           'name) + "." + ext is tried for each ext'),
            },
            'evidence': ('utils.tjs (data#1281) lines 486-505; '
                         'getExistImageNameForLayer() lines 511-513 tries '
                         '"png","jpg","tlg","tlg5","tlg6","bmp".'),
        },
        {
            'id': 'tjs.audio.extensions',
            'detail': {
                'kind': 'ext-list',
                'contexts': ['tjs:audio.storage', 'pbd:sename'],
                'exts': ['.ogg', '.opus', '.wav'],
                'preferred': '.ogg',
            },
            'evidence': ('utils.tjs (data#1281) lines 515-543 getExistVoice(): '
                         'tries arg0+".ogg", then ".opus", then ".wav"; '
                         'se.tjs (data#1264) lines 27-48 tries ".wav" -> '
                         '".ogg" -> ".opus" -> ".tcw" for SE storage.'),
        },
        {
            'id': 'tjs.kag.image.storage.exts',
            'detail': {
                'kind': 'ext-list',
                'contexts': ['tjs:image.storage'],
                'exts': ['.asd', '.png', '.tlg', '.bmp', '.jpg', '.emf',
                         '.stage', '.stand', '.sinfo', '.event'],
            },
            'evidence': ('kagenvironment.tjs (data#1230) line 105: '
                         'var _imageFileExtList = [".asd", ".png", ".tlg", '
                         '".bmp", ".jpg", ".emf", ".stage", ".stand", '
                         '".sinfo", ".event"]; line 106: _musicFileExtList = '
                         '[".wav", ".ogg", ".opus"];'),
        },
        {
            'id': 'tjs.ui.pbd.lang-suffix',
            'detail': {
                'kind': 'template',
                'contexts': ['tjs:ui.pbd', 'tjs:ui.file'],
                'templates': ['{base}_{lang}.pbd', '{base}.pbd'],
                'note': ('GetUIFile(base, ext, lang) = base + "_" + lang + ext '
                         'when that file exists, else the original name; '
                         'GetUIImport(base) = GetUIFile(base, ".pbd", lang)'),
            },
            'evidence': ('uisupport.tjs (data#496) lines 88-100 GetUIFile(): '
                         'local2 = string(local0) + "_" + string(arg2) + '
                         'string(local1); if (Storages.isExistentStorage('
                         'local2)) return local2; return arg0;  and lines '
                         '103-108 GetUIImport(base) -> GetUIFile(base, '
                         '".pbd", *).'),
        },
        {
            'id': 'tjs.ui.image.layer-suffix',
            'detail': {
                'kind': 'template',
                'contexts': ['tjs:ui.image'],
                'templates': ['{base}_{layer}'],
            },
            'evidence': ('uisupport.tjs (data#496) lines 116-120 GetUIImage(): '
                         'var local0 = arg0 + "_" + arg1; if '
                         '(getExistImageName(local0) != "") return local0; '
                         'return arg0;'),
        },
        {
            'id': 'tjs.system-menu.ks',
            'detail': {
                'kind': 'suffix',
                'contexts': ['tjs:setSystemMenuStorage'],
                'suffix': '.ks',
                'note': 'setSystemMenuStorage(x) -> storage x + ".ks"',
            },
            'evidence': ('uimain.tjs (data#491) line 1434: '
                         'setSystemMenuStorage(local0 + ".ks"); and line '
                         '1534: setSystemMenuStorage("search.ks"); -- KAG '
                         'resolves the menu storage with a ".ks" suffix.'),
        },
        {
            'id': 'tjs.autopath.directories',
            'detail': {
                'kind': 'dir-list',
                'contexts': ['tjs:addAutoPath'],
                'dirs': [p.rstrip('/') + '/' for p in AUTOPATH_PREFIXES],
                'note': ('initialize.tjs addArchive(name) -> '
                         'Storages.addAutoPath(name + "/"); the list below is '
                         'the exact archive / folder list of the game'),
            },
            'evidence': ('initialize.tjs (data#1223) lines 405-441: '
                         'local0("k2compat"); local0("system"); '
                         'local0("sysscn"); var local1 = ["video","others",'
                         '"rule","sound","bgm","fgimage","bgimage","scenario",'
                         '"scn","image","voice","face","init","font","sysse",'
                         '"main","evimage","thum","uipsd","motion","motiondx",'
                         '"emote","emotedx","bishamon",void]; '
                         'while(...) addArchive(local3); if (gpu_enable) '
                         'addArchive("glsl");  with addArchive(arg0) -> '
                         'Storages.addAutoPath(arg0 + "/") (lines 31-39).'),
        },
        {
            'id': 'tjs.autopath.archive-root-prefix',
            'detail': {
                'kind': 'path-strip',
                'contexts': ['tjs:execStorage', 'tjs:KAGLoadScript',
                             'tjs:include'],
                'prefixes': [p for p in AUTOPATH_PREFIXES],
                'note': ('a storage name that starts with an auto-path prefix '
                         'is stored in the archive *without* it: '
                         'Scripts.execStorage("system/Initialize.tjs") -> '
                         'initialize.tjs (system/ is mounted by '
                         'initialize.tjs); k2compat/ is mounted by '
                         'k2compat.tjs and k2compat_<name>.tjs entries are at '
                         'the archive root'),
            },
            'evidence': ('startup.tjs (data#1035) line 9: '
                         'Scripts.execStorage("system/Initialize.tjs"); '
                         'initialize.tjs (data#1223) lines 20-40 '
                         'useArchiveIfExists()/addArchive() mount '
                         'System.arcPath + name + ".xp3" and '
                         'Storages.addAutoPath(name + "/").  Verified: '
                         'hash("initialize.tjs") is in the table, '
                         'hash("system/Initialize.tjs") is not.'),
        },
        {
            'id': 'tjs.datapack.pbd',
            'detail': {
                'kind': 'suffix',
                'contexts': ['tjs:datapack', 'pbd:packbase'],
                'suffix': '.pbd',
                'note': ('TJS/ns0 data packs are .pbd files; the UI definition '
                         'PBD is named after its own packbase list: '
                         'packbase ["afterstory"] -> afterstory.pbd'),
            },
            'evidence': ('voice.tjs (data#1282) lines 169-212: '
                         '.pbd files are loaded with Scripts.loadDataPack(); '
                         'uistand.tjs (data#495) line 990: '
                         '_thumbCacheTable = Scripts.loadDataPack('
                         '"_chthum_index.pbd"); and the TJS/ns0 data itself: '
                         'every known UI definition has packbase == its own '
                         'file stem (afterstory -> afterstory.pbd, '
                         'option_cmds -> option_cmds.pbd, ...).'),
        },
        {
            'id': 'pbd.holdimage.pack.tlg',
            'detail': {
                'kind': 'template',
                'contexts': ['pbd:holdimage', 'pbd:storage'],
                'templates': ['{name}.tlg'],
                'note': ('UI definition PBDs reference their image packs by '
                         'storage/holdimage "<name>__pack"; the archive entry '
                         'is "<name>__pack.tlg" (verified for all 63 distinct '
                         'holdimage values of the tenshi PBDs).'),
            },
            'evidence': ('TJS/ns0 data tree walk (hxlib.formats.tjs); e.g. '
                         'backlog.pbd storage="backlog__pack" -> table entry '
                         '"backlog__pack.tlg"; 63/63 distinct holdimage values '
                         'match a real .tlg entry when suffixed with ".tlg".'),
        },
        {
            'id': 'pbd.layerinfo.sidecars',
            'detail': {
                'kind': 'sidecar',
                'contexts': ['pbd:layerFilename'],
                'exts': ['.png', '.tlg', '.pbd', '.sinfo', '.asd', '.mtn'],
                'note': ('layer-info PBDs (character / title PSB layer lists) '
                         'carry layerFilename values; those are storage stems '
                         'whose archive names are <stem>.<image ext>.'),
            },
            'evidence': ('tjs/ns0 layer lists under fgimage/upgrade/image '
                         '(e.g. かぐ耶a.pbd / かぐ耶a_0.pbd next to '
                         'かぐ耶a_0_<num>.tlg); 40/40 distinct layerFilename '
                         'values map to real entries with an image extension.'),
        },
        {
            'id': 'tjs.voice-volume-sidecars',
            'detail': {
                'kind': 'sidecar',
                'contexts': ['tjs:audio.storage'],
                'templates': ['{stem}.pbd', '{name}.vol.csv',
                              '{stem}.vol.csv'],
            },
            'evidence': ('voice.tjs (data#1282) lines 199-212 '
                         'getVoiceVolumeFile(arg0): tries '
                         'chopStorageExt(arg0)+".pbd", then arg0+".vol.csv", '
                         'then chopStorageExt(arg0)+".vol.csv"; loadCopyValueData '
                         'reads the .pbd as a data pack (line 186).'),
        },
        {
            'id': 'tjs.psd-sidecars',
            'detail': {
                'kind': 'sidecar',
                'contexts': ['tjs:psd.storage'],
                'exts': ['.psd', '.pimg', '.pbd', '.txt'],
            },
            'evidence': ('standinformation.tjs (data#1268) lines 1-4: '
                         'getExistPSDImageName(arg0) -> '
                         'getExistFileNameAutoExtFill(arg0, "psd", "pimg", '
                         '"pbd", "txt"); psdlayer.tjs (data#1262) line 668: '
                         'exts = [".psd", ".pimg", ".pbd", ".txt", ".tjs"].'),
        },
        {
            'id': 'tjs.asd.affinesource',
            'detail': {
                'kind': 'suffix',
                'contexts': ['tjs:image.storage'],
                'templates': ['{stem}.asd'],
            },
            'evidence': ('affinesourceasd.tjs (data#1171) line 577: '
                         'var local3 = (arg1.asdfile !== void) ? arg1.asdfile '
                         ': Storages.chopStorageExt(local0) + ".asd"; '
                         'affinesourceasd lane (data#1172) line 211 same.'),
        },
        {
            'id': 'tjs.sinfo.stand',
            'detail': {
                'kind': 'suffix',
                'contexts': ['tjs:standinfo'],
                'templates': ['{filename}.sinfo', '{sinfo}.sinfo'],
            },
            'evidence': ('standinformation.tjs (data#1268) lines 445-448: '
                         'var local6 = local2[local4].filename + ".sinfo"; if '
                         '(local2[local4].sinfo != "") local6 = '
                         'local2[local4].sinfo + ((Storages.extractStorageExt('
                         'local2[local4].sinfo) == "") ? ".sinfo" : "");'),
        },
        {
            'id': 'tjs.movie.exts',
            'detail': {
                'kind': 'ext-list',
                'contexts': ['tjs:movie.storage'],
                'exts': ['.mp4', '.wmv', '.mtn'],
                'suffixes': ['_censored'],
            },
            'evidence': ('uisupport.tjs (data#496) lines 60-85 '
                         'SelectMovieFile(): tries chopStorageExt(arg0) + '
                         '"_censored", arg0 + ".mp4" when '
                         'Storages.isExistentStorage(arg0 + ".mp4");'),
        },
        {
            'id': 'tjs.k2compat.include',
            'detail': {
                'kind': 'template',
                'contexts': ['tjs:include', 'tjs:KAGLoadScript'],
                'templates': ['k2compat_{name}.tjs'],
                'note': ('Krkr2CompatUtils.require(name) -> include(scriptBase '
                         '+ "k2compat_" + string(name) + ".tjs"); scriptBase = '
                         '"k2compat/" which is auto-path mounted, so the stored '
                         'entry name is k2compat_<name>.tjs'),
            },
            'evidence': ('k2compat.tjs (data#224) lines 20-38: function '
                         'include(arg0) { ... Scripts.execStorage(arg0); }  '
                         'function require(arg0) { if (arg0 == "" || '
                         'scriptLoaded[arg0]) return; ... include(scriptBase + '
                         '("k2compat_" + string(arg0) + ".tjs")); }  with '
                         'var scriptBase = "k2compat/"; (line 29).'),
        },
        {
            'id': 'tjs.plugin.dll',
            'detail': {
                'kind': 'suffix',
                'contexts': ['tjs:Plugins.link'],
                'suffix': '.dll',
                'names': ['PackinOne.dll', 'menu.dll', 'win32dialog.dll',
                          'psbfile.dll', 'messenger.dll', 'fstat.dll'],
            },
            'evidence': ('k2compat.tjs (data#224) lines 140-160 '
                         'Plugins.link(...); uiextra.tjs (data#489) line 2221: '
                         'Plugins.link("psbfile.dll"); initialize.tjs '
                         '(data#1223) line 384: local0 = "PackinOne.dll".'),
        },
        {
            'id': 'tjs.sprintf.numbered-names',
            'detail': {
                'kind': 'template',
                'contexts': ['tjs:dynamic'],
                'templates': ['{prefix}%04d_%02d', '{prefix}_quick_%02d',
                              '{name}%08x%08x.png'],
                'note': 'runtime-generated names: not enumerable from data',
            },
            'evidence': ('uistand.tjs (data#495) lines 1563/632/634: '
                         '"%s%08x%08x.png".sprintf(...), '
                         '"_%04d_%02d.".sprintf(...), '
                         '"_quick_%02d.".sprintf(...); used for auto thumbnails '
                         '(the 88 residual .png entries are of this family).'),
        },
        # ---- phase 2: runtime-composed names ---------------------------
        {
            'id': 'tjs.lang.tag_suffix',
            'detail': {
                'kind': 'suffix',
                'contexts': ['tjs:lang.file'],
                'langs': list(LANGUAGE_TAGS),
                'templates': ['{base}_{lang}.{ext}'],
                'note': ('GetSystemLangFile(file) tries <stem>_<CurrentLanguage'
                         'Tag><ext> then <stem>_en<ext>; the language list is '
                         '["jp","en","cn","tw"] and jp falls back to the base '
                         'file name'),
            },
            'evidence': ('yuzu_default.tjs (text data#502) line 410: '
                         '.multiLangLanguageTags = [ "jp", "en", "cn", "tw" ]; '
                         'multilang.tjs (data#1104) lines 1393-1413 '
                         'GetSystemLangFile(); syslangtext.tjs (data#1151) '
                         'line 1 LoadSysLangText(arg0="syslangtext_", '
                         'arg1=".ini") loops the same list -> '
                         'syslangtext_<tag>.ini; speech_dlg.tjs (data#1144) '
                         'getHelpTextFile() delegates to it.'),
        },
        {
            'id': 'tjs.lang.ui_image',
            'detail': {
                'kind': 'suffix',
                'contexts': ['tjs:ui.image'],
                'langs': list(NONJP_LANGS),
                'templates': ['{base}_{lang}' + ext for ext in IMAGE_EXT_NAMES],
            },
            'evidence': ('uisupport.tjs (data#496) lines 116-120 GetUIImage('
                         'base, layer) -> base+"_"+layer then image auto-ext; '
                         'lines 453-455 uilangimage(base) -> GetUIImage(base, '
                         'CurrentLanguageUITag); custom config (text data#457) '
                         'SystemConfig.LangImageUIReplace lists the fixed UI '
                         'images (attention/aboutdialog/deactivelogo/'
                         'sqscrmask/title_logo/extra_watermark) that get '
                         'arg0.lang = CurrentLanguageUITag (uisupport.tjs '
                         'lines 363-393).'),
        },
        {
            'id': 'tjs.transition_script',
            'detail': {
                'kind': 'suffix',
                'contexts': ['tjs:transition'],
                'templates': ['trans_{name}.tjs'],
            },
            'evidence': ('world.tjs (data#1284) line 7641: getTransObject(x) = '
                         'getParameter(x, "transitions", "trans_"); '
                         '_getParameter() lines 7525-7541 builds '
                         'string(prefix) + name + ".tjs" and loads it with '
                         'Scripts.evalStorage(); the name domain is '
                         'this.transitionName (world.tjs lines 3257-3280).'),
        },
        {
            'id': 'tjs.k2compat.delayload',
            'detail': {
                'kind': 'suffix',
                'contexts': ['tjs:k2compat'],
                'templates': ['k2compat_{name}.tjs'],
            },
            'evidence': ('k2compat.tjs (data#224) lines 184-194: '
                         'delayLoadFunction/delayLoadSingleton/'
                         'delayLoadProperty build require(\'<name>\') and '
                         'require() (lines 32-38) does include(scriptBase + '
                         '"k2compat_" + name + ".tjs"); call sites include '
                         'delayLoadSingleton("console"/"controller"), '
                         'delayLoadFunction("inputstring"), '
                         'delayLoadProperty("pad").'),
        },
        {
            'id': 'tjs.ui.thumb_bgthum',
            'detail': {
                'kind': 'template',
                'contexts': ['tjs:thumb.bgthum'],
                'templates': ['bgthum_{stage}.jpg'],
            },
            'evidence': ('uistand.tjs (data#495) lines 2015-2017 '
                         'getThumbCacheRealFile(): return "bgthum_" + arg0 + '
                         '"." + arg3 (arg3 = "jpg", line 2010); the stage '
                         'argument comes from the stage table '
                         '(text data#e00003: "stages" => %["image" => '
                         '"<name>TIME"], "times" => %[<time> => %[prefix => '
                         '"m"/"a"/"b"/"c"/"E"/...]]).'),
        },
        {
            'id': 'tjs.ui.thumb_chthum',
            'detail': {
                'kind': 'suffix',
                'contexts': ['pbd:thumb'],
                'data_pack': '_chthum_index.pbd',
                'templates': ['{stem}.png'],
            },
            'evidence': ('uistand.tjs (data#495) lines 1558-1567: local5 = '
                         '"chthum_%s_".sprintf(curChar); local8 = '
                         '"%s%08x%08x.png".sprintf(...); the data pack '
                         '_chthum_index.pbd (upgrade#1413) stores each chthum '
                         'stem verbatim as its values (442 leaves, 178 unique).'),
        },
        {
            'id': 'tjs.ui.thumb_thum',
            'detail': {
                'kind': 'suffix',
                'contexts': ['pbd:thumb'],
                'templates': ['{stem}.png', '{stem}_{lang}.png'],
                'langs': list(NONJP_LANGS),
            },
            'evidence': ('CG-mode list (text data#e00451: header '
                         '"#サムネール,CG名称" and rows "thum_ev101,...") and '
                         'save-screen table (text data#e00474); thum stems get '
                         '.png and the UI-language variants used by '
                         'uilangimage (see tjs.lang.ui_image).'),
        },
        {
            'id': 'tjs.fonts.embedded',
            'detail': {
                'kind': 'template',
                'contexts': ['tjs:font'],
                'templates': ['font1_${size}.tft', 'font2_${size}.tft',
                              'font3it_${size}.tft', 'font4_${size}.tft',
                              'font4it_${size}.tft', 'sysfont_%d.tft',
                              'headfont_%d.tft', 'keyfont_%d.tft',
                              'SourceHanSans{JP,TC,SC}-{Regular,Bold,Heavy}.'
                              'otf', 'SourceHanSerif{JP,TC,SC}-{Bold,Heavy}.'
                              'otf', 'Kosugi-Regular.ttf',
                              'KosugiMaru-Regular.ttf',
                              '{atlas_stem}.png', '{atlas_stem}.tjs'],
                'sizes': {'shared': [56, 48, 39, 32, 26, 20, 14, 10],
                          'sysfont': [10, 12, 15, 16, 18, 20, 22, 24, 30],
                          'headfont': [20, 22, 24, 30, 36],
                          'keyfont': [14]},
            },
            'evidence': ('embfontlist.tjs (text data#124): "file"=>"font1_'
                         '${size}.tft" + shared size list, "sysfont_%d.tft" + '
                         'size list, "headfont_%d.tft", "keyfont_%d.tft" '
                         '(size 14), "atlas"=>"sysfont_atlas.png"; '
                         'embfontloader.tjs (data#1069) line 58: atlas file '
                         '(png/tlg) -> chopStorageExt(file) + ".tjs" -> '
                         '*_atlas.tjs.  Licence/notice files are the '
                         '"license"/"detail" values.'),
        },
        {
            'id': 'tjs.movie.lang_prefix',
            'detail': {
                'kind': 'template',
                'contexts': ['tjs:movie.storage'],
                'templates': ['{lang}_{base}' + ext for ext in
                              ('.mp4', '.wmv', '.mtn')],
                'note': 'non-jp language builds prefix the language tag before '
                        'the movie storage stem',
            },
            'evidence': ('uisupport.tjs (data#496) lines 61-85 '
                         'SelectMovieFile(): var local0 = (_notjp ? '
                         'CurrentLanguageTag + "_" : "") + '
                         'Storages.chopStorageExt(arg0); then '
                         'getExistFileNameAutoExtFill(... "wmv","mpg","mpeg",'
                         '"mp4") / arg0 + ".mp4"; scenario tags '
                         '"[sysmovie file=ed1_noa]" supply the stems.'),
        },
        {
            'id': 'pbd.layer.zero_variant',
            'detail': {
                'kind': 'template',
                'contexts': ['pbd:filename', 'pbd:layerFilename'],
                'templates': ['{stem}.pbd', '{stem}_0.pbd'],
            },
            'evidence': ('Character layer-info PBDs ship as a sibling trio '
                         'per layer set: かぐ耶a.pbd / かぐ耶a.sinfo / '
                         'かぐ耶a_0.pbd (fgimage arc; .sinfo stores '
                         'filename:\'かぐ耶a\', standinformation.tjs data#1268 '
                         'reads it). All 25 missing *_0.pbd entries are proven '
                         'by this rule.'),
        },
        {
            'id': 'tjs.append_setup',
            'detail': {
                'kind': 'template',
                'contexts': ['tjs:append'],
                'templates': ['append_setup_{n}.tjs',
                              'append_setup_{n}_patch.tjs'],
            },
            'evidence': ('append_init.tjs (data#1040) lines 435-469 '
                         'ForeachAppendFileList(): local7 = chopStorageExt('
                         'arg0) + "_" + <append number> + extractStorageExt('
                         'arg0) (plus "_patch" variant); the numbers come from '
                         'AppendDataConfig "patch_append0"/"patch_append1" '
                         '(text data#e00482).'),
        },
        {
            'id': 'tjs.ui.title_func_sidecar',
            'detail': {
                'kind': 'suffix',
                'contexts': ['tjs:ui.func'],
                'suffix': '.func',
            },
            'evidence': ('uimain.tjs (data#491) lines 286-303 property storage: '
                         '["title"] + add("title_noclear"/"title_allage") + '
                         '"title_lang_" + (isLeft?"left":"right"); the .func '
                         'sidecar is loaded by uisystem.tjs line 2345: '
                         'chopStorageExt(ui storage) + ".func".'),
        },
        {
            'id': 'tjs.ui.glyph_sidecar',
            'detail': {
                'kind': 'suffix',
                'contexts': ['tjs:storage.stem'],
                'templates': ['{stem}.png', '{stem}.tlg', '{stem}.asd',
                              '{stem}.pbd'],
            },
            'evidence': ('uitext.tjs (data#1254) line 779/782 lineBreakGlyph = '
                         '"linebreak" / pageBreakGlyph = "pagebreak" (loaded as '
                         'images, with affinesource .asd sidecars); '
                         'autoskipmark.tjs (data#1041) lines 132-140 '
                         'AutoSkipMarkStorage "autoskipmark" / '
                         'AutoSkipMarkPosStorage "autoskipmark_pos".'),
        },
        {
            'id': 'tjs.filter.ini',
            'detail': {
                'kind': 'suffix',
                'contexts': ['misc.filter_ini'],
                'suffix': '.ini',
            },
            'evidence': ('Voice/sound config tables reference named audio '
                         'filters (voice/*.txt "[voeff name=... filter=<name>]" '
                         'and custom config "MovieAudioSampleFilter = '
                         '"wf_reverb""); the filter definitions are the '
                         'dsp_*/rv_*/dl_*/wf_*.ini entries (wave filter list in '
                         'SoundBuffer / Config.tjs).'),
        },
    ]


# ===========================================================================
# CLI (debug helper)
# ===========================================================================
def _main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import json
    from pathlib import Path
    ap = argparse.ArgumentParser(
        description='Analyse decompiled TJS2 sources for file-name facts')
    ap.add_argument('paths', nargs='+', help='decompiled .tjs files or dirs')
    ap.add_argument('--json', action='store_true', help='print stats as JSON')
    args = ap.parse_args(argv)
    sources: dict[str, str] = {}
    for p in map(Path, args.paths):
        files = [p] if p.is_file() else sorted(p.glob('*.tjs'))
        for f in files:
            sources[f.name] = f.read_text(encoding='utf-8', errors='replace')
    facts = list(analyze_sources(sources))
    report = last_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print('sources=%(sources)d call_sites=%(call_sites)d facts_total=%(facts_total)d'
              % report)
        print('api calls:', dict(sorted(report['api_calls'].items(),
                                        key=lambda kv: -kv[1])))
        print('api resolved:', dict(sorted(report['api_resolved'].items(),
                                           key=lambda kv: -kv[1])))
    for fact in facts:
        print('%s\t%s\t%s\t%s' % (fact.kind, fact.value, fact.context,
                                  fact.source))
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
