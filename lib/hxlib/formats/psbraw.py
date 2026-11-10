"""psbraw.py -- 用 PSB 物件樹抽「遊戲自己存的明文檔名候選」（結構化字串來源）。

PSB（``PSB\\0`` v3）是 Kirikiri/TJS2 的序列化容器。這裡用 vendor
``psb_parser.PSBReader`` 把整棵物件樹讀出來，遞迴走訪所有字串葉子，並且
**只留下像 storage 名稱的值**（完整檔名、目錄、語音 id、符號開頭的音效名、
ASCII storage 樣板），把劇情文字、UI 文案等大量非檔名字串濾掉。

為什麼這是「邏輯」而不是關鍵字探測：

* 抽出的每個值都是 PSB 裡**實際存在的字串**，附帶它在物件樹中的 key
  （``storage`` / ``file`` / ``filename`` / ``voice`` …），provenance 可回溯。
* 之後由 rules.py 依「引擎原始碼證明的副檔名規則」補齊；本模組不猜副檔名。

完整的 PSB 欄位語意解析（command tag / scnchart 等）由 ``psb.py`` 負責；
本模組是它的保險絲：即使語意解析漏了某種 PSB，字串來源仍不會整批漏掉。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

try:
    from ..facts import Fact, KIND_DIR, KIND_FRAGMENT, KIND_NAME
except ImportError:  # pragma: no cover
    from facts import Fact, KIND_DIR, KIND_FRAGMENT, KIND_NAME

MAGIC = b'PSB\x00'
KINDS = ('psb',)

# --------------------------------------------------------------------- filters
_KNOWN_EXT_RE = re.compile(
    r'\.(?:tlg|png|jpg|jpeg|webp|bmp|psb|pimg|ogg|opus|wav|mp3|flac|m4a|tjs|ks|'
    r'scn|sli|csv|txt|json|ini|toml|dat|bin|stand|sinfo|stage|pbd|mtn|asd|tft|'
    r'otf|ttf|mp4|wmv|func|psd|dll)$', re.IGNORECASE)
VOICE_ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9]{1,15}_\d{3}_\d{4}[A-Za-z]?$')
# storage 名稱允許的字元（路徑、底線、等號、連字號；不含空白/引號/程式符號）
_STORAGE_RE = re.compile(r'^[A-Za-z0-9_./\-]{2,96}$')
# 音效/特效名：符號開頭 + 日文（→→走る／学校／靴、≪エフェクト35s、★気合、†吸血…）
_SYMBOL_RE = re.compile(
    r'^[\u2190-\u21ff\u25a0-\u25ff\u2600-\u27bf\u266a\u266b\u2020\u2021\u00a1-\u00bf'
    r'\u3010\u3011\uff01-\uff60\uffe0-\uffee](?=[^\s"\']{1,60}$)[^\s"\'<>,;:]{1,60}$')
# 日文對話/文案的常見字元：含這些就不太可能是檔名
_SENTENCE_CHARS = set('。、！？!?「」『』（）()…‥：；\u3000')
_FILE_KEYS = frozenset({
    'storage', 'uistorage', 'file', 'files', 'filename', 'file_name', 'refile',
    'path', 'src', 'url', 'resource', 'archive', 'module', 'sename', 'holdimage',
    'packbase', 'voice', 'se', 'bgm', 'sound', 'movie', 'music', 'image',
    'graphic', 'cell', 'asdfile', 'sinfo', 'stand', 'pbd', 'psb',
})
_SKIP_KEYS = frozenset({'text', 'texts', 'message', 'caption', 'title', 'lines',
                        'data', 'action', 'actions', 'commands', 'tags', 'words'})

_VENDOR_TOOLS_READY = False


def _ensure_vendor_tools() -> None:
    """vendor 的 psb_parser.py 在 tools/ 底下（不在 src/common），要加進 sys.path。"""
    global _VENDOR_TOOLS_READY
    if _VENDOR_TOOLS_READY:
        return
    import vendor
    tools = Path(vendor.src()).parent / 'tools'
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    _VENDOR_TOOLS_READY = True


def _reader_for(data: bytes):
    """取得 PSB 物件樹：優先用專案自己的 psb.PSBReader（正確 UTF-8），
    退而用 vendor psb_parser。"""
    try:
        from .psb import PSBReader as OwnReader
        r = OwnReader(data)
        return r.read_object_tree() if hasattr(r, 'read_object_tree') else r.parse()
    except Exception:
        pass
    _ensure_vendor_tools()
    import psb_parser
    r = psb_parser.PSBReader.__new__(psb_parser.PSBReader)
    r.filepath = '<memory>'
    r._data = data
    r.header = psb_parser.parse_header(data)
    r._name_arrays = None
    r._names = None
    r._string_offsets = None
    r._strings = None
    r._chunk_offsets = None
    r._chunk_lengths = None
    return r.read_object_tree()


def _iter_tree_strings(node, key=None):
    if isinstance(node, str):
        yield key, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _iter_tree_strings(v, k if isinstance(k, str) else key)
    elif isinstance(node, (list, tuple)):
        for x in node:
            yield from _iter_tree_strings(x, key)


def _classify(key: str | None, value: str) -> tuple[str, str] | None:
    v = value.strip()
    if len(v) < 2 or len(v) > 120:
        return None
    if any(ch in _SENTENCE_CHARS for ch in v) or '\n' in v or '\r' in v:
        return None
    if _KNOWN_EXT_RE.search(v):
        return KIND_NAME, 'psb:name'
    if v.endswith('/'):
        return KIND_DIR, 'psb:dir'
    if key in _SKIP_KEYS:
        return None
    if VOICE_ID_RE.match(v):
        return KIND_FRAGMENT, 'psb:voice'
    if key in _FILE_KEYS:
        return KIND_FRAGMENT, 'psb:%s' % (key or 'value')
    if _SYMBOL_RE.match(v) and len(v) <= 48:
        return KIND_FRAGMENT, 'psb:se'
    if _STORAGE_RE.match(v) and len(v) <= 64:
        return KIND_FRAGMENT, 'psb:value'
    return None


def iter_facts(entry, data: bytes):
    if not data or not data.startswith(MAGIC):
        return
    try:
        tree = _reader_for(data)
    except Exception:
        return
    source = '%s#%d' % (getattr(entry, 'archive', '?'),
                       getattr(entry, 'index', -1))
    seen: set[str] = set()
    for key, value in _iter_tree_strings(tree):
        if value in seen:
            continue
        seen.add(value)
        # KAG storage 可帶 `檔案|標籤` 語法（語音 noa_115_0094|RV_風呂）：兩邊都試
        parts = [value]
        if '|' in value:
            parts = [p.strip() for p in value.split('|') if p.strip()]
        for part in parts:
            got = _classify(key, part)
            if got is None:
                continue
            kind, ctx = got
            yield Fact(value=part, kind=kind, source=source,
                       note='psb key=%s' % key, context=ctx)
