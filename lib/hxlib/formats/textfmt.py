"""textfmt.py -- 文字型 entry 的解碼與結構化檔名抽取。

KiriKiri 遊戲把很多「名字的來源」放在純文字／半結構化資料裡：

* `!scnlist.txt` / `_scnheads.txt`：一行一個場景檔名（直接就是 NAME）。
* `*.csv`：`cglist` / `imagediffmap` / `charvoice` / `soundlist` / `savelist` …
  每一欄幾乎都是檔名或檔名片段（`ev101aa`、`ec__logo_dot|ec_noa.pimg`、
  `noa_%s_%04d` 這種樣板）。
* `base.stage` / `*.stand` / `*.sinfo`：TJS 字面量，`filename:'xxx'`、
  `"image" => "xxxTIME_SEASON"`。
* `.ks`（KAG 腳本）：`@call storage="version.ks"`、`[addSysScript storage=start]`。
* `.sli`：音效 loop 資訊。

本模組**只做「把檔案裡實際存在的字串抽出來」**，並附上語意 context
（`kag:storage`、`csv:col1`、`stage:image`、`line`…）。
要不要補副檔名、要不要展開樣板，由 `rules.py` 依「遊戲程式邏輯證據」決定。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

try:
    from ..facts import Fact, KIND_DIR, KIND_FRAGMENT, KIND_NAME  # noqa: E402
except ImportError:  # 直接以檔案路徑載入時（測試 / 工具）
    from facts import Fact, KIND_DIR, KIND_FRAGMENT, KIND_NAME  # noqa: E402

# --------------------------------------------------------------------- decode
KNOWN_EXTS = (
    'tlg', 'png', 'jpg', 'jpeg', 'webp', 'bmp', 'psb', 'pimg', 'psd',
    'ogg', 'opus', 'wav', 'mp3', 'flac', 'm4a',
    'tjs', 'ks', 'scn', 'sli', 'csv', 'txt', 'json', 'ini', 'toml', 'dat',
    'bin', 'stand', 'sinfo', 'stage', 'pbd', 'mtn', 'asd', 'tft', 'otf', 'ttf',
    'mp4', 'wmv', 'func', 'mchx', 'psb', 'webp', 'cfg', 'conf', 'xml', 'yaml',
)
_FILE_TOKEN_RE = re.compile(
    r'[A-Za-z0-9_\-./]+\.(?:csv|txt|ini|tjs|ks|pbd|png|jpg|tlg|psd|ogg|opus|wav|'
    r'mp4|wmv|asd|tft|ttf|otf|mtn|scn|sinfo|stand|stage|sli|psb|pimg|toml|mchx|'
    r'mtrk|ajpm|dib|jif|jxr|tlg5|tlg6|func)')

_EXT_RE = re.compile(r'\.(%s)$' % '|'.join(sorted(set(KNOWN_EXTS), key=len, reverse=True)),
                     re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r'%[0-9]*[sdfx]|\$\{[^}]*\}|TIME|SEASON|COLOR')
_WS_RE = re.compile(r'[\r\n\t]')


def descramble(data: bytes) -> bytes:
    """0xFE 0xFE 開頭的文字 entry：KiriKiri 的「簡單 scramble」。"""
    if len(data) < 6 or data[:2] != b'\xfe\xfe':
        return data
    mode = data[2]
    body = bytearray(data[5:])
    if mode == 0:
        for i in range(0, len(body) - 1, 2):
            if body[i + 1] == 0 and body[i] < 0x20:
                continue
            body[i + 1] ^= (body[i] & 0xFE)
            body[i] ^= 1
    else:
        for i in range(0, len(body) - 1, 2):
            c = body[i] | (body[i + 1] << 8)
            c = ((c & 0xAAAA) >> 1) | ((c & 0x5555) << 1)
            body[i] = c & 0xFF
            body[i + 1] = (c >> 8) & 0xFF
    return bytes(body)


def decode_text(data: bytes) -> tuple[str, str] | None:
    """回傳 (text, encoding)；不是文字就回 None。"""
    if not data:
        return None
    if data[:2] == b'\xfe\xfe':
        data = descramble(data)
        enc = _guess_encoding(data)
        return (data.decode(enc, 'replace'), enc) if enc else None
    enc = _guess_encoding(data)
    if not enc:
        return None
    return data.decode(enc, 'replace'), enc


def _guess_encoding(data: bytes) -> str | None:
    head = data[:4096]
    if head.startswith(b'\xef\xbb\xbf'):
        return 'utf-8-sig'
    if head.startswith(b'\xff\xfe'):
        return 'utf-16le'
    if head.startswith(b'\xfe\xff'):
        return 'utf-16be'
    # UTF-16LE（無 BOM）：大量 0x00 或合理碼位
    zeros = head[1::2].count(0)
    if len(head) >= 8 and zeros > len(head[1::2]) * 0.2:
        txt = head.decode('utf-16le', 'replace')
        good = sum(1 for ch in txt if ch in '\r\n\t' or 0x20 <= ord(ch) < 0xfff0)
        if good > len(txt) * 0.85:
            return 'utf-16le'
    try:
        head.decode('utf-8')
        return 'utf-8'
    except UnicodeDecodeError:
        pass
    try:
        txt = head.decode('cp932', 'replace')
    except Exception:
        return None
    good = sum(1 for ch in txt if ch in '\r\n\t' or 0x20 <= ord(ch) < 0xfff0)
    if good > len(txt) * 0.9 and (b'\n' in head or b'\r' in head):
        return 'cp932'
    return None


def probe_doc_kind(text: str) -> str:
    """依內容的「結構特徵／檔案自帶的表頭」判斷這是哪一種遊戲資料檔。

    這很重要：不同資料檔的欄位語意不同（`"x_…" => "s401"` 是場景表、
    `"image" => "…"` 是背景表、`@tag storage=` 是 KAG 腳本…），
    規則（rules.py）要靠這個決定怎麼補字尾。

    認的是「檔案自己宣告的格式」（開頭註解就寫了這是什麼表），不是檔名，
    所以對同引擎的其他遊戲也通用。
    """
    if not text:
        return 'text'
    head = text[:20000]
    # --- Yuzusoft / KAG 資料檔：靠檔案內的表頭宣告辨識 ---------------------
    if 'Sound Loop Information' in head or head.lstrip().startswith('#2.00'):
        return 'sli'
    if '#CGモード一覧' in head or ('サムネール' in head and 'CG名称' in head):
        return 'cglist'
    if 'アイキャッチ鑑賞用' in head or ('imagediffmap' in head.lower()):
        return 'imagediffmap'
    if '回想モード一覧' in head:
        return 'scenelist'
    if '話者に対するボイス割り当て' in head:
        return 'charvoice'
    if 'システム効果音定義' in head:
        return 'sysse'
    if 'システムトランジション上書き' in head:
        return 'systrans'
    if re.search(r'^#?\S*ファイル名\s*,\s*タイトル', head, re.M):
        return 'soundlist'
    if 'ジャンプ先ストレージ' in head or 'タブ区切りテキスト' in head:
        return 'scnlist'
    if '開始するシナリオを列挙' in head:
        return 'scnheads'
    if '画像の条件による差し替え' in head:
        return 'imageevalmap'
    if '画像名条件マッピング' in head:
        return 'imagenamemap'
    if 'セーブ置き換え' in head:
        return 'savelist'
    if 'Sound Loop Information' in head:
        return 'sli'
    # --- 結構判定 ---------------------------------------------------------
    if '(const) %[' in head or ('【' in head and '=> "s' in head):
        return 'chart'
    if '[texts]' in head or '[debug]' in text[:2000]:
        return 'uitexts'
    if _KAG_TAG_RE.search(head):
        return 'kag'
    if ('with (' in head or 'function ' in head or 'var ' in head) and (
            ' => ' in head or ';' in head):
        return 'tjsrc'
    if ('filename' in head and ("=>" in head or ':' in head)):
        return 'stand'
    if '"stages"' in text or '"times"' in text or '"prefix" =>' in text:
        return 'stage'
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) >= 4:
        csvy = sum(1 for l in lines[:200] if ',' in l or '\t' in l)
        if csvy >= len(lines[:200]) * 0.6:
            return 'csv'
    if any('=' in l and not l.startswith(';') for l in lines[:50]):
        return 'ini'
    return 'text'


_KAG_TAG_RE = re.compile(r'(?:^|[\[@])(?:sys|add|call|jump|link|load|image|bg|fg|se|voice|'
                         r'movie|dialog|panel|history|meswin|selopt|eval|emb|wait|'
                         r'macro|textwrite|beginskip|endskip|clearallmacro|tft)[A-Za-z]*\b')


# ---------------------------------------------------------------- extraction
_KAG_ATTR_RE = re.compile(
    r'(?:^|[\s\[@])([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*'
    r'(?:"([^"\r\n]*)"|\'([^\'\r\n]*)\'|([^\s\]\r\n,;]+))')
_QUOTED_RE = re.compile(r'"([^"\r\n]{1,200})"|\'([^\'\r\n]{1,200})\'')
_KEYVAL_RE = re.compile(
    r'"([^"\r\n]{1,120})"\s*=>\s*"([^"\r\n]{1,200})"|'
    r'\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*[\'"]([^\'"\r\n]{1,200})[\'"]')
_FILENAME_FIELD_RE = re.compile(r"filename\s*:\s*'([^'\r\n]{1,200})'")
_ROW_SPLIT_RE = re.compile(r'[,\t]')
_PIPE_SPLIT_RE = re.compile(r'\|')

# 目錄/檔名前綴常見但「只是路徑」的值不當檔名展開
_DIRECTORY_LIKE = re.compile(r'^[A-Za-z0-9_./-]+/$')


def _classify(value: str, context: str) -> tuple[str, str]:
    """回傳 (kind, 更精確的 context)。"""
    if value.endswith('/'):
        return KIND_DIR, context
    if _EXT_RE.search(value):
        return KIND_NAME, context
    if _PLACEHOLDER_RE.search(value):
        return KIND_FRAGMENT, context + ':template'
    return KIND_FRAGMENT, context


def _clean(value: str) -> str | None:
    v = value.strip().strip('\ufeff')
    if not v or len(v) > 200:
        return None
    if any(ch in v for ch in '\x00\r\n\t'):
        return None
    return v


class _Collector:
    """同一份文字內去重；同一 value 保留「語意最強」的 context。"""

    # context 優先度：數字越小越優先
    _PRIORITY = {
        'kag:storage': 0, 'kag:file': 0, 'kag:filename': 0, 'kag:module': 1,
        'stage:image': 0, 'stand:filename': 0, 'stage:value': 1,
        'line': 3, 'quote': 4, 'csv': 5,
    }

    def __init__(self, source: str) -> None:
        self.source = source
        self.items: dict[str, tuple[str, str, str]] = {}

    def add(self, raw: str, context: str, note: str = '') -> None:
        value = _clean(raw)
        if not value:
            return
        kind, ctx = _classify(value, context)
        old = self.items.get(value)
        if old is not None:
            if self._rank(ctx) <= self._rank(old[1]):
                return
        self.items[value] = (kind, ctx, note)

    @classmethod
    def _rank(cls, ctx: str) -> int:
        base = ctx.split(':', 1)[0]
        for key, prio in cls._PRIORITY.items():
            if ctx.startswith(key):
                return prio
        return 2 if base in ('kag', 'stage', 'stand') else 6

    def facts(self):
        for value, (kind, ctx, note) in self.items.items():
            yield Fact(value=value, kind=kind, source=self.source,
                       note=note, context=ctx)


def _scan_line(col: _Collector, line: str, lineno: int) -> None:
    s = line.strip()
    if not s:
        return
    src_note = 'line %d' % lineno
    # 0) KAG 標籤名稱（tag 本身就是語意，例如 image/bg/se/voice/call）
    tag = ''
    tm = re.search(r'[\[@]([A-Za-z_]\w*)', s)
    if tm:
        tag = tm.group(1).lower()
    # 1) KAG 標籤屬性：@call storage="x" / [addSysScript ... storage=x]
    for m in _KAG_ATTR_RE.finditer(s):
        attr = m.group(1)
        val = m.group(2) if m.group(2) is not None else (
            m.group(3) if m.group(3) is not None else m.group(4))
        ctx = ('kag:%s.%s' % (tag, attr)) if (tag and attr in (
            'storage', 'uistorage', 'file', 'filename', 'path', 'icon')) else ('kag:' + attr)
        if attr in ('storage', 'uistorage', 'file', 'filename', 'module', 'path',
                    'image', 'prefix', 'suffix', 'refile', 'icon'):
            col.add(val, ctx, src_note)
        elif attr in ('name', 'target2'):
            col.add(val, 'kag:' + attr, src_note)
    # 2) TJS 字面量：filename:'x' / "image" => "x" / key: "x"
    for m in _FILENAME_FIELD_RE.finditer(s):
        col.add(m.group(1), 'stand:filename', src_note)
    for m in _KEYVAL_RE.finditer(s):
        if m.group(1) is not None:
            key, val = m.group(1), m.group(2)
        else:
            key, val = m.group(3), m.group(4)
        if key in ('image',):
            ctx = 'stage:image'
        elif key in ('prefix', 'suffix'):
            ctx = 'stage:prefix'
        else:
            ctx = 'stage:value'
            # 字典的 key 本身也可能是檔名（例如 scnchartdata 的場景表）
            col.add(key, 'stage:key', src_note)
        col.add(val, ctx, src_note)
    # 3) 引號字串（沒有 key 的）
    for m in _QUOTED_RE.finditer(s):
        col.add(m.group(1) if m.group(1) is not None else m.group(2),
                'quote', src_note)
    # 4) CSV / TSV / '|' 分隔欄位
    cells = [c.strip().strip('"\'') for c in _ROW_SPLIT_RE.split(s)]
    if len(cells) > 1:
        for i, cell in enumerate(cells):
            if not cell:
                continue
            for part in _PIPE_SPLIT_RE.split(cell):
                part = part.strip()
                if part:
                    col.add(part, 'csv:col%d' % i, src_note)
    # 4b) 設定檔的 `tag = 檔名 : 參數` 語法（sysse.ini / dsp_*.ini / *.ini…）
    m = re.match(r'^\s*[^=\s\]]+\s*=\s*(.+)$', s)
    if m:
        rhs = m.group(1).strip()
        base = re.split(r'\s*:\s*', rhs, 1)[0].strip().strip('"\'')
        if base and base != rhs:
            col.add(base, 'kv:value', src_note)
        elif base:
            col.add(base, 'kv:value', src_note)
    # 5) 整行（清單檔一行一個檔名）
    if len(cells) == 1:
        col.add(s, 'line', src_note)
    elif s.startswith('#'):
        col.add(s.lstrip('# ').strip(), 'line', src_note)


def iter_text_facts(data: bytes, source: str) -> list[Fact]:
    """把一份文字 entry 內所有像名字的字串抽成 Fact。"""
    decoded = decode_text(data)
    if decoded is None:
        return []
    text, _enc = decoded
    doc = probe_doc_kind(text)
    col = _Collector(source)
    for lineno, line in enumerate(text.splitlines(), 1):
        try:
            _scan_line(col, line, lineno)
        except Exception:
            continue
    out: list[Fact] = []
    for fact in col.facts():
        ctx = fact.context or ''
        if doc and doc != 'text' and not ctx.startswith(doc + ':'):
            ctx = doc + ':' + ctx
        out.append(Fact(value=fact.value, kind=fact.kind, source=fact.source,
                        note=fact.note, context=ctx))
    # 全文字掃「檔名 token」：設定/註解裡常直接寫 data/main/xxx.csv 這種字面值
    seen = {f.value for f in out}
    for tok in _FILE_TOKEN_RE.findall(text)[:4000]:
        tok = tok.strip('.')
        if not tok or len(tok) > 120:
            continue
        cands = [tok]
        if '/' in tok:
            cands.append(tok.rsplit('/', 1)[-1])
        for cand in cands:
            if cand in seen or not _EXT_RE.search(cand):
                continue
            seen.add(cand)
            out.append(Fact(value=cand, kind=KIND_NAME, source=source,
                            note='文字中直接出現的檔名 token',
                            context='text:token'))
    return out


# pipeline 統一介面：KINDS + iter_facts(entry, data)
KINDS = ('text',)


def iter_facts(entry, data) -> list[Fact]:
    """pipeline 介面：entry = hxlib.corpus.CorpusEntry。"""
    source = '%s#%d' % (getattr(entry, 'archive', '?'),
                       getattr(entry, 'index', -1))
    return iter_text_facts(data, source)


# ------------------------------------------------------------------- probes
def looks_like_name_list(text: str, *, min_lines: int = 3) -> bool:
    """判斷一份文字是不是「一行一個檔名」的清單（給 pipeline 統計用）。"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    named = sum(1 for l in lines if _EXT_RE.search(l) and ' ' not in l)
    return named >= min_lines and named >= len(lines) * 0.6


def split_template(template: str) -> tuple[str, str, int] | None:
    """把 `noa_%s_%04d` / `xxx%02d` 這種樣板拆成 (prefix, suffix, width)。

    回傳的 width 是最後一個 `%0?Nd` 的 N（找不到回 0）；`%s` 視為不定寬。
    """
    m = re.search(r'%0?(\d*)d', template)
    if m:
        width = int(m.group(1)) if m.group(1) else 0
        return template[:m.start()], template[m.end():], width
    return None
