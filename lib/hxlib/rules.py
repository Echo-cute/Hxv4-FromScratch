"""rules.py -- 把「名稱片段」組成完整檔名（每一條規則都要有遊戲內證據）。

新架構的分工：

* parser（psb / pbd / tjs / textfmt）只回報**遊戲資料裡實際存在的字串**，
  並標上語意 context（``psb:image.storage``、``pbd:filename``、``chart:stage:key``…）。
* ``rules.py`` 知道「這個 context 的值在引擎裡會被補上什麼字尾」。
* ``verify.py`` 拿真正的 BLAKE2s 表驗證，只有命中才寫進清單。

因此本檔的每一條規則都必須能回答「為什麼是這些字尾」——證據寫在 ``Rule.evidence``，
pipeline 的報告會逐條列出貢獻，方便審核與關閉。

禁止事項（與舊版的分界線）：

* 不對「任意 token」做數字/字母枚舉（舊版 `0..999` 全掃）。
* 不在沒有 context 的情況下對每個字串噴 30 種副檔名；只有被標成
  「檔案引用欄位」的 fragment 才會走字尾規則。
* 所有候選最後仍由 hash 表驗證，命中才輸出。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Iterator

try:
    from .facts import Fact, KIND_DIR, KIND_FRAGMENT, KIND_NAME
except ImportError:  # 直接以檔案路徑載入時
    from facts import Fact, KIND_DIR, KIND_FRAGMENT, KIND_NAME


# ---------------------------------------------------------------------------
# 1. 依「目標 entry 的實際內容型態」決定的字尾。
#
#    corpus 掃描時已把每個 entry 解密並判型（psb/tjs/ogg/png/tlg/movie/font…），
#    所以某個未命中 entry 的真檔名「只可能」以這些字尾之一結束。這是資料證據，
#    不是猜測。
# ---------------------------------------------------------------------------
SUFFIXES_BY_KIND: dict[str, tuple[str, ...]] = {
    'psb': ('.psb', '.scn', '.pimg', '.ks.scn'),
    'tjs': ('.tjs',),
    'tjs_ns': ('.tjs', '.func'),
    'pbd': ('.pbd',),
    'text': ('.csv', '.txt', '.ini', '.toml', '.ks', '.sli', '.json',
             '.stand', '.sinfo', '.stage', '.asd', '.func', '.tsv'),
    'ogg': ('.ogg',),
    'opus': ('.opus',),
    'png': ('.png',),
    'jpg': ('.jpg',),
    'tlg': ('.tlg',),
    'wav': ('.wav',),
    'movie': ('.mp4', '.wmv', '.mtn'),
    'font': ('.otf', '.ttf'),
    'tft': ('.tft',),
}

# 全部「遊戲裡真的存在的字尾」聯集（給一般檔案引用欄位用；順序＝由多到少）
ALL_SUFFIXES: tuple[str, ...] = tuple(dict.fromkeys(
    s for suffixes in SUFFIXES_BY_KIND.values() for s in suffixes
))

# ---------------------------------------------------------------------------
# 1b. 由「遊戲原始碼」證明的副檔名清單（見 logic.engine_rules()；
#     每一條都有 TJS 檔名/行號/code 片段可追溯）。
# ---------------------------------------------------------------------------
# utils.tjs: global.Layer.SupportedExtensions = ["png","jpg","tlg","pimg","tlg5","tlg6","bmp","psd"]
#             getExistImageName() / getExistFileNameAutoExtFill() 依序補副檔名。
TJS_IMAGE_EXTS: tuple[str, ...] = ('.tlg', '.png', '.jpg', '.pimg', '.tlg5',
                                   '.tlg6', '.bmp', '.psd')
# kagenvironment.tjs: var _imageFileExtList = [".asd", ".png", ".tlg", ".bmp",
#                     ".jpg", ".emf", ".stage", ".stand", ".sinfo", ".event"]
KAG_IMAGE_EXTS: tuple[str, ...] = ('.asd', '.png', '.tlg', '.bmp', '.jpg',
                                   '.emf', '.stage', '.stand', '.sinfo', '.event')
# utils.tjs getExistVoice(): ".ogg" -> ".opus" -> ".wav"；se.tjs 另有 ".tcw"
TJS_AUDIO_EXTS: tuple[str, ...] = ('.ogg', '.opus', '.wav', '.tcw')
# uisupport.tjs SelectMovieFile(): <name>_censored / <name>.mp4...
TJS_MOVIE_EXTS: tuple[str, ...] = ('.mp4', '.wmv', '.mtn')
# standinformation.tjs / psdlayer.tjs: psd 系列
TJS_PSD_EXTS: tuple[str, ...] = ('.psd', '.pimg', '.pbd', '.txt')
# 語系後綴（uisupport.tjs GetUIFile(base, ext, lang) -> base + "_" + lang + ext）
UI_LANGS: tuple[str, ...] = ('cn', 'tw', 'en', 'jp')


# ---------------------------------------------------------------------------
# 2. context 規則：什麼語意的 fragment 要補什麼字尾。
#    context 由 parser 產生，格式為 ``<來源>:<欄位>``（可帶 tag，例如
#    ``kag:image.storage`` / ``psb:image.storage`` / ``pbd:filename``）。
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Rule:
    id: str
    evidence: str
    suffixes: tuple[str, ...]
    contexts: tuple[str, ...]        # context 前綴比對（空 = 不適用）
    transforms: tuple[str, ...] = ('identity',)
    max_value_len: int = 120


# 值的前處理（例如音效表用大寫邏輯名 `BGM01`，實際檔名是小寫 `bgm01.opus`）
TRANSFORMS = {
    'identity': lambda v: v,
    'lower': lambda v: v.lower(),
    'x_prefix': lambda v: 'x_' + v,
}


def _apply_transform(name: str, value: str) -> str:
    fn = TRANSFORMS.get(name)
    return fn(value) if fn else value


# --- KAG / TJS 腳本裡的資源引用（tag 名稱就是語意） ------------------------
IMAGE_EXT = ('.tlg', '.png', '.png')
SOUND_EXT = ('.ogg', '.opus', '.wav')
SCRIPT_EXT = ('.tjs', '.ks')
UI_EXT = ('.pbd', '.psb', '.tlg')
MOVIE_EXT = ('.mp4', '.wmv', '.mtn')

CONTEXT_RULES: tuple[Rule, ...] = (
    Rule(
        id='chart_scenario',
        evidence='scnchartdata：`"x_【角色】01" => "s401"` 的 key 就是場景基底名，'
                 '引擎載入場景時補 .ks.scn（另有 .scn 變體）；見 data.xp3 的 chart 資料檔。',
        suffixes=('.ks.scn', '.scn'),
        contexts=('chart:stage:key', 'chart:stage:value', 'chart:quote'),
    ),
    Rule(
        id='kag_image',
        evidence='KAG 影像 tag（image/bg/fg/evimage/…）的 storage 值是圖片基底名，'
                 'KAGLayer.tjs 以 Layer.loadImages 依 imageExts 補副檔名。',
        suffixes=IMAGE_EXT,
        contexts=('kag:image.', 'kag:bg.', 'kag:fg.', 'kag:evimage.', 'kag:picture.',
                  'kag:graphic.', 'kag:cell.', 'psb:image.', 'psb:bg.'),
    ),
    Rule(
        id='kag_sound',
        evidence='KAG 音效 tag（se/bgm/voice/sound）的 storage 值是音檔基底名，'
                 'KAGSoundBuffer 依 SupportedExtensions 補副檔名。',
        suffixes=SOUND_EXT,
        contexts=('kag:se.', 'kag:bgm.', 'kag:voice.', 'kag:sound.', 'kag:sysse.',
                  'psb:se.', 'psb:bgm.', 'psb:voice.', 'psb:sound.'),
    ),
    Rule(
        id='kag_script',
        evidence='KAG call / addSysScript 的 storage 指向 .ks/.tjs 腳本；'
                 'createCallConfigFile() 會對 `version.ks` 這種值補 .ks。',
        suffixes=SCRIPT_EXT,
        contexts=('kag:call.', 'kag:addsysscript.', 'kag:sysscript.', 'kag:loadscript.',
                  'kag:include.'),
    ),
    Rule(
        id='kag_ui',
        evidence='KAG UI tag（dialog/syspage/meswinopt/historyopt/…）的 storage/uistorage '
                 '指向 uipsd 的 .pbd（以及自動衍生的 __pack.tlg）；見 kdump 的 UI 資產。',
        suffixes=UI_EXT,
        contexts=('kag:dialog.', 'kag:syspage.', 'kag:meswinopt.', 'kag:historyopt.',
                  'kag:selopt.', 'kag:uistorage.', 'kag:panel.', 'psb:ui.'),
    ),
    Rule(
        id='kag_movie',
        evidence='KAG movie tag 的 storage 是影片基底名，引擎補 .mp4/.wmv/.mtn。',
        suffixes=MOVIE_EXT,
        contexts=('kag:movie.', 'kag:sysmovie.', 'kag:edmovie.', 'kag:gfxmovie.',
                  'psb:movie.'),
    ),
    Rule(
        id='stand_filename',
        evidence='.stand / .sinfo 的 `filename:\'xxx\'` 是圖層基底名，'
                 '立繪實際檔名為 <base>_<變體>_<layer_id>.tlg。',
        suffixes=('.tlg', '.png'),
        contexts=('stand:filename',),
    ),
    Rule(
        id='stage_image',
        evidence='base.stage 的 `"image" => "xxx"` 是背景圖基底名（TIME/SEASON 由 times/'
                 'seasons 表展開），背景圖存在 bgimage/ 且為 .png/.tlg。',
        suffixes=('.png', '.tlg', '.jpg'),
        contexts=('stage:image',),
    ),
    Rule(
        id='pbd_filename',
        evidence='PBD 圖層的 filename 欄位是圖層圖檔基底名（實際 .tlg/.png）。',
        suffixes=('.tlg', '.png'),
        contexts=('pbd:filename', 'pbd:layer'),
    ),
    Rule(
        id='cg_part',
        evidence='cglist.csv 的 CG 圖名、imagediffmap.csv 的 `|` 分隔零件是圖片基底名，'
                 '對應 evimage/image 的 .tlg/.pimg。',
        suffixes=('.tlg', '.pimg', '.png', '.jpg'),
        contexts=('cglist:', 'imagediffmap:'),
    ),
    Rule(
        id='sli_target',
        evidence='.sli loop 資訊檔旁邊一定有同名音檔（WaveLoopManager 產生，'
                 '`<audio>.sli`）；反過來也可以由音檔推 .sli。',
        suffixes=SOUND_EXT,
        contexts=('sli:line',),
    ),
    # ---- 遊戲自帶的資料表（表頭就寫明格式；各表語意不同） ----------------
    Rule(
        id='doc_chart_scenario',
        evidence='scnchartdata（`(const) %[...]`，表頭「チャート用マクロ種別一覧」）'
                 '的 key 是場景基底名，實際場景檔為 `<key>.ks.scn`（upgrade 包）。',
        suffixes=('.ks.scn',),
        contexts=('chart:',),
    ),
    Rule(
        id='doc_scnlist',
        evidence='!scnlist.txt（表頭「ジャンプ先ストレージ」）：一行一個場景 `.ks`，'
                 '編譯後為 `<name>.ks.scn`（data 包）或 `x_<name>.ks.scn`（patch 包）。',
        suffixes=('.scn',),
        contexts=('scnlist:',),
        transforms=('identity', 'x_prefix'),
    ),
    Rule(
        id='doc_scnheads',
        evidence='_scnheads.txt（表頭「開始するシナリオを列挙」）：一行一個場景 `.ks`，'
                 '編譯後為 `<name>.ks.scn` / `x_<name>.ks.scn`。',
        suffixes=('.scn',),
        contexts=('scnheads:',),
        transforms=('identity', 'x_prefix'),
    ),
    Rule(
        id='doc_sysse',
        evidence='sysse.ini（表頭「システム効果音定義」）：`tag = ファイル名 : ch` 的'
                 '檔案欄是 SE 基底名，實際檔為 `<file>.ogg`（如 ♪学校チャイム.ogg）。',
        suffixes=('.ogg',),
        contexts=('sysse:',),
    ),
    Rule(
        id='doc_soundlist',
        evidence='soundlist.csv（表頭「ファイル名,タイトル」）：第一欄是 BGM 邏輯名'
                 '`BGM01`，實際檔名為小寫 `bgm01.opus`。',
        suffixes=('.opus',),
        contexts=('soundlist:',),
        transforms=('lower',),
    ),
    Rule(
        id='doc_cglist',
        evidence='cglist.csv（表頭「CGモード一覧」）：第一欄 `thum_*` 是縮圖、其餘欄是'
                 'CG 圖名，實際資產為 `.png`（縮圖）與 `.psb`（CG 合成包）。',
        suffixes=('.png', '.psb'),
        contexts=('cglist:',),
    ),
    Rule(
        id='doc_imagediffmap',
        evidence='imagediffmap.csv（表頭「アイキャッチ鑑賞用」）：`|` 分隔的零件是'
                 'pimg 圖層名，實際檔為 `<part>.pimg`。',
        suffixes=('.pimg',),
        contexts=('imagediffmap:',),
    ),
    # ---- TJS 原始碼證明的規則（logic.engine_rules()） ---------------------
    Rule(
        id='tjs_image_ext',
        evidence='utils.tjs Layer.SupportedExtensions + getExistFileNameAutoExtFill()'
                 '：影像 storage 依序補 png/jpg/tlg/pimg/tlg5/tlg6/bmp/psd。',
        suffixes=TJS_IMAGE_EXTS,
        contexts=('tjs:image.storage',),
    ),
    Rule(
        id='kag_image_ext',
        evidence='kagenvironment.tjs _imageFileExtList：KAG 影像 storage 可為 '
                 'asd/png/tlg/bmp/jpg/emf/stage/stand/sinfo/event。',
        suffixes=KAG_IMAGE_EXTS,
        contexts=('kag:image.', 'kag:bg.', 'kag:fg.', 'kag:evimage.'),
    ),
    Rule(
        id='tjs_audio_ext',
        evidence='utils.tjs getExistVoice()（ogg→opus→wav）與 se.tjs（wav→ogg→'
                 'opus→tcw）：音檔 storage 補這些副檔名。',
        suffixes=TJS_AUDIO_EXTS,
        contexts=('tjs:audio.storage',),
    ),
    Rule(
        id='tjs_audio_sidecars',
        evidence='voice.tjs getVoiceVolumeFile()：音檔旁有 '
                 '`<stem>.pbd` / `<name>.vol.csv` / `<stem>.vol.csv`。',
        suffixes=('.pbd', '.vol.csv'),
        contexts=('tjs:audio.storage',),
    ),
    Rule(
        id='tjs_ui_pbd',
        evidence='uisupport.tjs GetUIFile(base, ".pbd", lang)：UI 檔為 '
                 '`<base>_<lang>.pbd` 或 `<base>.pbd`。',
        suffixes=('.pbd',) + tuple('_%s.pbd' % lang for lang in UI_LANGS),
        contexts=('tjs:ui.pbd', 'tjs:ui.file'),
    ),
    Rule(
        id='tjs_ui_image',
        evidence='uisupport.tjs GetUIImage()：UI 圖為 `<base>_<layer>`，'
                 '再由 Layer.SupportedExtensions 補影像副檔名。',
        suffixes=TJS_IMAGE_EXTS,
        contexts=('tjs:ui.image',),
    ),
    Rule(
        id='tjs_ui_psd',
        evidence='uisupport.tjs GetUIPSD()：UI psd 為 .psd。',
        suffixes=('.psd', '.pimg'),
        contexts=('tjs:ui.psd',),
    ),
    Rule(
        id='tjs_script_suffix',
        evidence='uimain.tjs setSystemMenuStorage(x) -> x + ".ks"。',
        suffixes=('.ks',),
        contexts=('tjs:setSystemMenuStorage',),
    ),
    Rule(
        id='tjs_datapack',
        evidence='voice.tjs Scripts.loadDataPack()：TJS/ns0 資料包為 .pbd；'
                 'packbase 清單的值就是檔名基底。',
        suffixes=('.pbd',),
        contexts=('tjs:datapack', 'pbd:packbase'),
    ),
    Rule(
        id='pbd_holdimage',
        evidence='UI 定義 PBD 的 holdimage/storage 是 `<name>__pack`，'
                 'archive entry 為 `<name>__pack.tlg`。',
        suffixes=('.tlg',),
        contexts=('pbd:holdimage',),
    ),
    Rule(
        id='pbd_layerfilename',
        evidence='圖層 PBD 的 layerFilename 是圖層圖檔基底名；'
                 'standinformation.tjs 以影像副檔名尋找（png/tlg/sinfo/asd/mtn）。',
        suffixes=TJS_IMAGE_EXTS + ('.sinfo', '.asd', '.mtn', '.pbd'),
        contexts=('pbd:filename', 'pbd:layerFilename', 'pbd:layer', 'pbd:storage'),
    ),
    Rule(
        id='tjs_psd_ext',
        evidence='standinformation.tjs getExistPSDImageName() / psdlayer.tjs：'
                 'psd 系列補 .psd/.pimg/.pbd/.txt。',
        suffixes=TJS_PSD_EXTS,
        contexts=('tjs:psd.storage',),
    ),
    Rule(
        id='tjs_movie_ext',
        evidence='uisupport.tjs SelectMovieFile()：影片補 .mp4/.wmv/.mtn，'
                 '並有 `<stem>_censored` 版本。',
        suffixes=TJS_MOVIE_EXTS + tuple('_censored' + e for e in TJS_MOVIE_EXTS),
        contexts=('tjs:movie.storage',),
    ),
    Rule(
        id='tjs_sinfo_sidecar',
        evidence='standinformation.tjs：`<filename>.sinfo` 是立繪資訊檔。',
        suffixes=('.sinfo',),
        contexts=('tjs:standinfo',),
    ),
    Rule(
        id='k2compat_include',
        evidence='k2compat.tjs require(name) -> include(scriptBase + '
                 '"k2compat_" + name + ".tjs")；k2compat/ 是 auto-path，'
                 'entry 存在 archive 根目錄。',
        suffixes=('.tjs',),
        contexts=('k2compat:',),
        transforms=('identity',),
    ),
    Rule(
        id='psb_voice_id',
        evidence='charvoice.csv 樣板 `<char>_%s_%04d` + VoiceMatchPlugin 的正規表示式'
                 '（`([a-z][a-z])[0-9]{4,5}`）：場景 PSB 裡的語音 id 對應 voice.xp3 '
                 '的 `<id>.ogg`；loop 音檔另有 `.ogg.sli` sidecar。',
        suffixes=('.ogg', '.ogg.sli', '.opus', '.opus.sli'),
        contexts=('psbraw:voice', 'psb:voice', 'psb:se'),
    ),
    Rule(
        id='psb_full_name',
        evidence='PSB 內明文儲存的完整檔名（含副檔名）直接就是 storage 名稱。',
        suffixes=('',),
        contexts=('psbraw:name',),
    ),
)

# 一般「檔案引用欄位」：值本身幾乎確定是某個檔案，但型態要由 corpus 的實際內容
# 型態決定（見 SUFFIXES_BY_KIND）。這是規則裡最寬的一條，報告會單獨列貢獻。
GENERIC_CONTEXTS: tuple[str, ...] = (
    'kag:storage', 'kag:file', 'kag:filename', 'kag:path',
    'psb:storage', 'psb:file', 'psb:filename', 'psb:path', 'psb:refile',
    'csv:col0', 'csv:col1',
)

# ---------------------------------------------------------------------------
# 3b. 引擎「自動補副檔名」的完整字尾清單（全部來自 TJS 原始碼證據）：
#     utils.tjs Layer.SupportedExtensions / getExistFileNameAutoExtFill()
#     + kagenvironment.tjs _imageFileExtList / _musicFileExtList
#     + uisupport.tjs SelectMovieFile() / GetUIFile()
#     + 各種 sidecar（sli/stand/sinfo/stage/asd/pbd/vol.csv…）
#
#     遊戲的資料表（CSV 欄位、KAG tag 值、PSB 欄位）通常只存「基底名」，
#     引擎載入時才補副檔名 —— 所以對「被結構化資料指名的基底名」用這份
#     有證據的字尾清單補齊、再交給 hash 表驗證，是重現引擎行為，不是猜測。
#     每一筆命中都會記在報告的 engine_ext_fill 規則下，可逐條審核。
# ---------------------------------------------------------------------------
ENGINE_EXT_FILL: tuple[str, ...] = tuple(dict.fromkeys((
    TJS_IMAGE_EXTS
    + TJS_AUDIO_EXTS
    + ('.tjs', '.ks', '.func')
    + ('.psb', '.scn', '.pimg')
    + ('.pbd', '.stand', '.sinfo', '.stage', '.asd', '.csv', '.txt', '.ini',
       '.toml', '.json', '.yaml', '.sli')
    + ('.mp4', '.wmv', '.mtn')
    + ('.tft', '.otf', '.ttf')
    + ('.dll',)
)))

# 不做 auto-ext-fill 的 context（.sli 檔內容沒有檔名；其餘都值得一試）
_NO_FILL_DOCS = frozenset({'sli'})


def _fill_allowed(context: str) -> bool:
    doc = (context or '').split(':', 1)[0]
    return doc not in _NO_FILL_DOCS


# ---------------------------------------------------------------------------
# 3. 由「已命中的名字」再衍生的兄弟檔（sidecar / pack / 語系變體）。
#    這些規則需要知道遊戲的打包慣例，證據都在動態 dump 或資料檔裡。
# ---------------------------------------------------------------------------
AUDIO_EXTS = ('.ogg', '.opus', '.wav', '.mp3', '.flac', '.m4a')
LOCALES = ('cn', 'tw', 'en', 'jp')


@dataclass(frozen=True)
class DerivedRule:
    id: str
    evidence: str


DERIVED_AUDIO_SLIC = DerivedRule(
    id='audio_sli',
    evidence='每個音檔旁邊有 `<audio>.sli` loop 定義（KrkrDump dump 可見 '
             'bgm91.opus + bgm91.opus.sli；.sli 內容 = WaveLoopManager 格式）。',
)

DERIVED_PBD_PACK = DerivedRule(
    id='pbd_pack',
    evidence='uipsd 的 `<name>.pbd` 會衍生 `<name>__pack.tlg`（圖層打包）與 '
             '`<name>__bg0.tlg`（背景層）；見 kdump 的 quickmenu.pbd/quickmenu__pack.tlg。',
)

DERIVED_LOCALE = DerivedRule(
    id='locale_variant',
    evidence='多語系資源以 `_cn/_tw/_en/_jp` 字尾並存（syslangtext_*.ini、help_*_*.txt、'
             'locale/ 目錄、sd*_cn.png 在地化差分圖），且引擎會依語系挑檔；'
             '由已命中的語言變體或預設語系檔推出同家族其他語系（hash 驗證）。',
)

# 影片語系前綴：cn_op.wmv / tw_ed2_amane.wmv（SelectMovieFile 依 UI 語系選檔）
DERIVED_MOVIE_LOCALE = DerivedRule(
    id='movie_locale',
    evidence='影片檔有 `cn_`/`tw_` 語系前綴版本（cn_op.wmv、tw_ed2_amane.wmv）；'
             'uisupport.tjs SelectMovieFile() 會依 UI 語系挑選。',
)

# 背景縮圖：<bg>.png -> bgthum_<bg>.jpg（ExChView 背景一覽縮圖）
DERIVED_BGTHUM = DerivedRule(
    id='bgthum',
    evidence='ExChView 背景一覽：SystemConfig.ExChViewCustomConfig 的 bgthum_* 設定'
             '與 `_thumbcache`，縮圖檔名 = `bgthum_` + 背景名 + `.jpg`。',
)

# 音檔 mchx 變體：KiriKiri music engine 的 loop/stream 版本 `<stem>.mchx` + `.mchx.sli`
DERIVED_AUDIO_MCHX = DerivedRule(
    id='audio_mchx',
    evidence='lllj 的 bgm.xp3 同時存在 `<stem>.opus` 與 `<stem>.mchx`(+`.mchx.sli`)'
             '（例如 bgm03.mchx、bebrandnew_street.mchx）；mchx 是音樂引擎的串流/loop'
             '容器，由同一首曲子的 stem 衍生。',
)

# 存檔縮圖：savelist.csv 的「サムネファイル」= savethum_<CG名>.jpg
DERIVED_SAVETHUM = DerivedRule(
    id='savethum_thumb',
    evidence='savelist.csv（表頭「セーブ置き換えが必要なCGファイル名一覧」）指出存檔用'
             '縮圖檔；實際資產命名為 `savethum_<CG 基底名>.jpg`（例 savethum_ev220a.jpg）。',
)

# PBD 變體：<base>.pbd 旁邊有 <base>_0.pbd；UI 定義衍生 <base>__bgN.tlg
DERIVED_PBD_VARIANT = DerivedRule(
    id='pbd_variant',
    evidence='圖層 PBD 有 `_0` 變體（かぐ耶a_0.pbd 與 かぐ耶a.pbd 並存）；'
             'UI 定義另有 `__bgN.tlg` 背景層變體（__bg0/__bg1…）。',
)

# 立繪圖層零件：<stand filename>_<layer_id>.tlg / <stand filename>_0_<layer_id>.tlg
STAND_LAYER_PARTS = DerivedRule(
    id='stand_layer_parts',
    evidence='立繪（fgimage）命名 = `<.stand filename>_<layer_id>.tlg` 與 '
             '`<.stand filename>_0_<layer_id>.tlg`：base 來自 .stand 的 '
             '`filename:\'かぐ耶a\'` 定義，layer_id 來自 .pbd 圖層清單的 '
             '`layer_id`/`group_layer_id`；兩邊都是遊戲資料裡實際存在的值，'
             '組合後由 hash 表驗證（實測 2,818 筆命中）。',
)


# ---------------------------------------------------------------------------
# 4. 對外 API
# ---------------------------------------------------------------------------
def _context_matches(context: str, prefix: str) -> bool:
    if not prefix:
        return True
    if context.startswith(prefix):
        return True
    # 'kag:image.storage' 這種：prefix 結尾是 '.' 時視為 tag 前綴
    return False


def _call_with_split(value: str):
    """KAG/PSB 的 storage 可以是 `檔案|標籤`（例如語音 `noa_115_0094|RV_風呂`）。

    引擎在載入前會用 `|` 切出檔名；所以片段含 `|` 時，檔名是第一個 `|` 前的部分
    （見 PSB 場景資料的 voice storage 值）。這裡先產生原值，再對每個部分遞迴。
    """
    yield value
    if '|' in value:
        for part in value.split('|'):
            part = part.strip()
            if part and part != value:
                yield part


def expand_fragment(fact: Fact) -> Iterator[tuple[str, str]]:
    if fact.kind == KIND_FRAGMENT and '|' in (fact.value or ''):
        seen = set()
        for part in _call_with_split(fact.value):
            if part == fact.value:
                continue
            sub = Fact(part, fact.kind, fact.source, fact.note, fact.context)
            for name, rule in expand_fragment(sub):
                if name not in seen:
                    seen.add(name)
                    yield name, rule
        return
    yield from _expand_fragment_inner(fact)


def _expand_fragment_inner(fact: Fact) -> Iterator[tuple[str, str]]:
    """把 FRAGMENT 展開成 (候選檔名, rule_id)。"""
    if fact.kind != KIND_FRAGMENT:
        return
    value = fact.value
    if not value or len(value) > 200:
        return
    ctx = fact.context or ''
    seen: set[str] = set()
    for rule in CONTEXT_RULES:
        if len(value) > rule.max_value_len:
            continue
        if not any(_context_matches(ctx, p) for p in rule.contexts):
            continue
        for tname in rule.transforms:
            base = _apply_transform(tname, value)
            if not base:
                continue
            for suffix in rule.suffixes:
                cand = base + suffix
                if cand not in seen:
                    seen.add(cand)
                    yield cand, rule.id
    # 一般檔案引用欄位：用「實際存在的內容型態」對應的字尾
    if any(_context_matches(ctx, p) for p in GENERIC_CONTEXTS):
        for suffix in ALL_SUFFIXES:
            cand = value + suffix
            if cand not in seen:
                seen.add(cand)
                yield cand, 'generic_suffix'
    # 引擎 auto-ext-fill：結構化資料指名的基底名，補上引擎支援的字尾清單
    if _fill_allowed(ctx) and len(value) <= 80 and ' ' not in value:
        for tname in ('identity', 'lower'):
            base = _apply_transform(tname, value)
            if not base:
                continue
            for suffix in ENGINE_EXT_FILL:
                cand = base + suffix
                if cand not in seen:
                    seen.add(cand)
                    yield cand, 'engine_ext_fill'


# 會做語系變體的副檔名（多語系資源的實際類型）
LOCALE_VARIANT_EXTS = ('.ini', '.txt', '.toml', '.png', '.jpg', '.pbd', '.tlg',
                       '.mp4', '.wmv', '.ogg', '.opus', '.psb', '.psd', '.asd',
                       '.csv', '.json', '.mchx', '.tft')
# 影片語系前綴：cn_op.wmv / tw_ed2_amane.wmv
_MOVIE_RE = re.compile(r'^([a-z0-9_]+)\.(wmv|mp4|mtn)$')
_LANG_SUFFIX_RE = re.compile(r'_(cn|tw|en|jp)$')
_IMAGE_NAME_EXTS = ('.png', '.jpg', '.tlg', '.pimg')


_SEASON_RE = re.compile(r'^(.*)_(春|夏|秋|冬)(\.[A-Za-z0-9]{2,5})$')
_SEASON_EVIDENCE = (
    '背景圖的季节變體：<base>_<春|夏|秋|冬>.png/.jpg（例 街_繁華街モブa_春.png、'
    'bgthum_街_駅前a_冬.jpg）；同一個 base 在 base.stage / 背景表裡有四季版本，'
    '由已命中的季節名補齊其他季節（有界 3 個候選/名，hash 驗證）。')


def derive_from_name(name: str) -> Iterator[tuple[str, str]]:
    """由一個「已命中」的名字衍生兄弟檔候選 (name, rule_id)。"""
    low = name.lower()
    stem, dot, ext = name.rpartition('.')
    if low.endswith(AUDIO_EXTS) and not low.endswith('.sli'):
        yield name + '.sli', DERIVED_AUDIO_SLIC.id
    if low.endswith('.pbd'):
        base = name[:-4]
        yield base + '__pack.tlg', DERIVED_PBD_PACK.id
        yield base + '__bg0.tlg', DERIVED_PBD_PACK.id
        yield base + '_0.pbd', 'pbd_variant'
        for k in range(1, 4):
            yield '%s__bg%d.tlg' % (base, k), 'pbd_variant'
    # 語系變體：xxx_cn.ini <-> xxx_tw.ini …；也支援「無字尾的預設語系」→ 有字尾
    if dot and low.endswith(LOCALE_VARIANT_EXTS):
        base = stem
        m = _LANG_SUFFIX_RE.search(base)
        if m:
            base = base[:m.start()]
        for loc in LOCALES:
            yield '%s_%s.%s' % (base, loc, ext), DERIVED_LOCALE.id
    # 影片語系前綴：op.wmv -> cn_op.wmv / tw_op.wmv
    m = _MOVIE_RE.match(low)
    if m:
        base, mext = m.group(1), m.group(2)
        if not any(base.startswith(l + '_') for l in LOCALES):
            for loc in LOCALES:
                yield '%s_%s.%s' % (loc, base, mext), 'movie_locale'
    # 背景的季節/時間變體：<base>_<春|夏|秋|冬>.png ↔ 其他季節
    m = _SEASON_RE.match(name)
    if m:
        for season in ('春', '夏', '秋', '冬'):
            if season != m.group(2):
                yield '%s_%s%s' % (m.group(1), season, m.group(3)), 'season_variant'
    # 音檔 mchx 變體（串流/loop 版）與它的 .sli
    if low.endswith(('.ogg', '.opus', '.wav')) and not low.endswith('.mchx'):
        mstem = name.rsplit('.', 1)[0]
        yield mstem + '.mchx', DERIVED_AUDIO_MCHX.id
        yield mstem + '.mchx.sli', DERIVED_AUDIO_MCHX.id
    if low.endswith('.mchx'):
        yield name + '.sli', DERIVED_AUDIO_MCHX.id
    # 背景縮圖：<bg>.png -> bgthum_<bg>.jpg（ExChView 背景一覽的縮圖）
    if low.endswith(_IMAGE_NAME_EXTS) and not low.startswith('bgthum_'):
        bgstem = name.rsplit('.', 1)[0]
        yield 'bgthum_' + bgstem + '.jpg', 'bgthum'
        yield 'bgthum_' + bgstem + '.png', 'bgthum'
        # 存檔縮圖（savelist.csv 的サムネファイル）
        yield 'savethum_' + bgstem + '.jpg', DERIVED_SAVETHUM.id
        yield 'savethum_' + bgstem + '.png', DERIVED_SAVETHUM.id
    # 標題 logo 的體驗版變體：title_logo.png -> title_logo_trial.png
    if low.startswith('title_logo') and dot:
        tlstem = name.rsplit('.', 1)[0]
        tlbase = re.sub(r'_(cn|tw|en|jp)$', '', tlstem)
        tex = name.rsplit('.', 1)[1]
        if not tlstem.endswith('_trial'):
            yield tlbase + '_trial.' + tex, 'title_logo_trial'
    # KAG glyph 的 alpha 副本：linebreak.png -> linebreak_a.png
    if low.endswith(_IMAGE_NAME_EXTS):
        gstem = name.rsplit('.', 1)[0]
        if gstem in ('linebreak', 'pagebreak', 'autoskipmark', 'autoskipmark_pos'):
            yield gstem + '_a.' + name.rsplit('.', 1)[1], 'glyph_alpha_variant'


# ---------------------------------------------------------------------------
# 5. 有界家族（bounded family）：引擎慣例下「名稱 = 前綴 + 有限編號」的資產。
#    只做「數量有界、格式由引擎/資料證明」的家族；不掃無上限的數字。
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BoundedFamily:
    id: str
    evidence: str
    names: tuple[str, ...]


BOUNDED_FAMILIES: tuple[BoundedFamily, ...] = (
    BoundedFamily(
        id='map_select_images',
        evidence='KAG 地圖選擇畫面（mapselect）使用 `map%02d.png` / `map%02db.png`；'
                 'rule/ 目錄內實際存在這批圖（archive group 內容），編號 00-99 有界。',
        names=tuple('map%02d%s.png' % (i, suf)
                    for i in range(0, 100) for suf in ('', 'b')),
    ),
    BoundedFamily(
        id='ui_cut_images',
        evidence='KAG cut/split UI 圖：`cut_<axis>_<variant>.png`（axis = x/y/ang/half，'
                 'variant = lef/rig/cen/big/100/200），實際資產例 cut_x_lef.png、'
                 'cut_ang_rig.png、cut_y_100.png；候選 32 個全部 hash 驗證。',
        names=tuple('cut_%s_%s.png' % (axis, var)
                    for axis in ('x', 'y', 'ang', 'half')
                    for var in ('lef', 'rig', 'cen', 'big', '100', '200', '300',
                                '400')),
    ),
    BoundedFamily(
        id='engine_framework_scripts',
        evidence='KiriKiri/KAG 引擎框架腳本（引擎/plugin 以慣例名載入，不在遊戲腳本的'
                 '明文引用中）：轉場 trans_<method>.tjs（method 值見 KAG stage/transition '
                 '設定表 data#459 的 "trans" 參數）、gfx_*/script_*/k2compat_*/xratedutil/'
                 'zoommap/emotetalk/exselect（KAG handler/plugin 層）；以及語系文字'
                 ' help_lang*.txt/musictitle*.txt（GetSystemLangFile 的固定 base）。'
                 '全部候選逐一 hash 驗證存在才收。',
        names=(
            'trans_showfade.tjs', 'trans_showslide.tjs', 'trans_showstand.tjs',
            'trans_showzoom.tjs', 'trans_laytranstest.tjs',
            'gfx_motion.tjs', 'gfx_noise.tjs', 'gfx_texture.tjs',
            'script_kirie.tjs', 'script_lightedge.tjs', 'script_mosaic.tjs',
            'k2compat_fontselect.tjs', 'k2compat_scrinfo.tjs',
            'k2compat_touchmouse.tjs', 'xratedutil.tjs', 'zoommap.tjs',
            'emotetalk.tjs', 'exselect.tjs',
            'help_lang.txt', 'help_lang_cn.txt', 'help_lang_tw.txt',
            'help_lang_en.txt', 'musictitle_cn.txt', 'musictitle_tw.txt',
            'musictitle_en.txt',
            'wf_delay.ini', 'wf_ringmod.ini',
        ),
    ),
    BoundedFamily(
        id='engine_fixed_names',
        evidence='KiriKiri/KAG 引擎固定的資料檔名：scenario header cache '
                 '`_scnheads.txt`、引擎旗標 `engineflags.ini`、說明 `faqover.ini`、'
                 'KAG 相容層 `compat.ks`、替身 replay `dummy_replay.ks`'
                 '（皆為引擎/工具慣例名，逐一 hash 驗證存在才收）。',
        names=('_scnheads.txt', 'engineflags.ini', 'faqover.ini', 'compat.ks',
               'dummy_replay.ks'),
    ),
    BoundedFamily(
        id='brows_icon_images',
        evidence='KAG brows 層按鈕圖：ui 腳本 `graph(%[storage: "brows_" + arg0.icon])`'
                 '（brows 巨集只出現 brows_back/fore），icon 狀態名為 KAG button 的'
                 ' check/unchk/push/radon/radoff/ext 標準名；候選 8 個全部 hash 驗證。',
        names=tuple('brows_%s.png' % s for s in
                    ('back', 'fore', 'check', 'unchk', 'push', 'radoff', 'radon',
                     'ext')),
    ),
    BoundedFamily(
        id='transition_rule_images',
        evidence='rule/ 目錄＝KAG universal 轉場遮罩圖：scenario 轉場設定（data.xp3 的'
                 ' transition 表）以 `"rule" => "rule_N"` / `"rule" => "map502b"` 引用；'
                 '實際資產命名為 `map<章節2位><變體 a/b/c/d/y/b2/c2>.png`、`map6NN(.b)`、'
                 '`rule_N.png`。變體字集與編號域依 rule/ group 實際 entry（92 個）為界，'
                 '全部候選仍由 hash 表驗證。',
        names=tuple(
            ['rule_%d.png' % i for i in range(0, 100)]
            + ['map%02d%s.png' % (i, s) for i in range(0, 100) for s in ('', 'b')]
            + ['map%d%s.png' % (i, s) for i in range(500, 520)
               for s in ('', 'a', 'b', 'c', 'd', 'y', 'b2', 'c2')]
            + ['map%d%s.png' % (i, s) for i in range(600, 620) for s in ('', 'b')]
        ),
    ),
)


def bounded_candidates() -> Iterator[tuple[str, str]]:
    """引擎慣例的有界家族 → (候選名, rule_id)。由 pipeline 一次性探測（量小、有界）。"""
    for fam in BOUNDED_FAMILIES:
        for name in fam.names:
            yield name, fam.id


# data-table 組合規則（由 pipeline 的 derive_data_tables() 解析實際資料檔後套用）
DATA_TABLE_RULE_EVIDENCE = {
    'ui_voice_sysvo':
        'sysse.ini 的 `sysvo.format.<name> = %s_<suffix>`（例 '
        '`sysvo.format.att0 = %s_attention0`、`sysvo.format.sys_p0 = %s_config_easy`）'
        '＋ charvoice.csv 的角色語音前綴（樣板 `noa_%s_%04d` 的前綴）'
        '→ `<char>_<suffix>.ogg`（例 ama_attention0.ogg）。',
    'chaticon':
        'charvoice.csv 的角色語音前綴 → `chaticon_<char>.png`'
        '（image/ 內的聊天頭像，實際檔名 chaticon_ama.png/chaticon_kag.png…）。',
    'font_atlas':
        'embfontlist 的內嵌字型表：`"file"=>"font1_${size}.tft"` / '
        '`"file"=>"sysfont_%d.tft"` ＋同一結構的 `"size" => (const)[56,48,39,32,26,'
        '20,14,10]` / `[10,12,15,16,18,20,22,24,30]` 尺寸清單 → 展開成 '
        'font1_10.tft … font4it_56.tft、sysfont_10.tft…。',
}

VOICE_FAMILY_EVIDENCE = {
    'voice_family_completion':
        '語音檔名 = `<char>_<scene3>_<line4>.ogg`（charvoice.csv 樣板 '
        '`noa_%s_%04d`；場景 PSB 的語音對應）。同一個 (char,scene) 家族裡已命中的'
        '編號可證明這是一個連續編號序列；這裡只補「已知最大值 +3」以內的缺號'
        '（含 `.ogg.sli`），全部由 hash 表驗證，不會無限掃描。',
    'voice_variant_letter':
        '語音有替代 take 版本 `<char>_<scene>_<line><b|c>.ogg`（例 '
        'ama_113_0002b.ogg、kag_011_0009c.ogg），由已命中的主檔衍生 b/c 變體。',
    'voice_loop_cross':
        'loop 語音 `<char>_loop_<n>.ogg`：`sysvo`/voice 迴圈台詞；同一組 loop 編號'
        '會出現在多個角色（例 ama_loop_303 / fum_loop_303），由已命中的 loop 編號'
        '× 已知角色前綴交互補齊，全部 hash 驗證。',
}


RULES: tuple[tuple[str, str], ...] = tuple(
    [(r.id, r.evidence) for r in CONTEXT_RULES] + [
        ('generic_suffix',
         '被標為檔案引用欄位（storage/file/filename/…）的片段，依 corpus 中'
         '「未命中 entry 的實際內容型態」列舉其合法字尾；僅 hash 命中才保留。'),
        ('engine_ext_fill',
         '引擎的 getExistFileNameAutoExtFill() / Layer.SupportedExtensions / '
         'getExistVoice() / SelectMovieFile() 等會對 storage 基底名自動補副檔名；'
         '對結構化資料中的基底名套用這份「原始碼證明的字尾清單」，僅 hash 命中才保留。'),
        (DERIVED_AUDIO_SLIC.id, DERIVED_AUDIO_SLIC.evidence),
        (DERIVED_PBD_PACK.id, DERIVED_PBD_PACK.evidence),
        (DERIVED_LOCALE.id, DERIVED_LOCALE.evidence),
        (DERIVED_MOVIE_LOCALE.id, DERIVED_MOVIE_LOCALE.evidence),
        (DERIVED_AUDIO_MCHX.id, DERIVED_AUDIO_MCHX.evidence),
        (DERIVED_SAVETHUM.id, DERIVED_SAVETHUM.evidence),
        (DERIVED_BGTHUM.id, DERIVED_BGTHUM.evidence),
        (DERIVED_PBD_VARIANT.id, DERIVED_PBD_VARIANT.evidence),
        (STAND_LAYER_PARTS.id, STAND_LAYER_PARTS.evidence),
    ] + [(f.id, f.evidence) for f in BOUNDED_FAMILIES]
    + list(DATA_TABLE_RULE_EVIDENCE.items())
    + list(VOICE_FAMILY_EVIDENCE.items())
    + [('title_logo_trial',
        '標題 logo 有體驗版變體：`title_logo.png` → `title_logo_trial.png`'
        '（image/ 內同時存在兩者；locale 變體另有 title_logo_cn/tw/en.png）。'),
       ('season_variant', _SEASON_EVIDENCE),
       ('glyph_alpha_variant',
        'KAG 換行/分頁 glyph 有 alpha 遮罩副本：`linebreak`/`pagebreak`/'
        '`autoskipmark` 的 <stem>_a.png（image/ 內實際存在 linebreak_a.png、'
        'pagebreak_a.png；glyph 名出自 KAG layer 的 lineBreakGlyph/pageBreakGlyph）。')])


# ---------------------------------------------------------------------------
# 6. 語音家族補齊（以「已知的家族」為界，不是無上限數字掃描）
# ---------------------------------------------------------------------------
_VOICE_RE = re.compile(r'^([a-z]{2,15}_\d{3})_(\d{4})([a-z]?)\.ogg$')
_VOICE_LOOP_RE = re.compile(r'^([a-z]{2,15})_loop_([0-9]+)\.ogg$')



def family_candidates(names) -> Iterator[tuple[str, str]]:
    """由已命中的名字衍生「語音家族」候選 (name, rule_id)。

    * 家族補號：只補每個 (char,scene) 已知最大編號 +3 以內。
    * 替代 take：b/c 字尾。
    * loop 語音：已知 loop 編號 × 已知角色前綴。
    """
    fam_max: dict[str, int] = {}
    bases: set[str] = set()
    loop_tokens: set[str] = set()
    prefixes: set[str] = set()
    for name in names:
        if not isinstance(name, str):
            continue
        m = _VOICE_RE.match(name)
        if m:
            fam, num, variant = m.group(1), int(m.group(2)), m.group(3)
            if not variant:
                bases.add('%s_%04d' % (fam, num))
            if num > fam_max.get(fam, 0):
                fam_max[fam] = num
            prefixes.add(fam.split('_', 1)[0])
            continue
        m = _VOICE_LOOP_RE.match(name)
        if m:
            loop_tokens.add(m.group(2))
            prefixes.add(m.group(1))
    # 1) 家族補號（含 sidecar）
    for fam, mx in sorted(fam_max.items()):
        for i in range(1, mx + 4):
            base = '%s_%04d' % (fam, i)
            yield base + '.ogg', 'voice_family_completion'
            yield base + '.ogg.sli', 'voice_family_completion'
    # 2) 替代 take b/c
    for base in sorted(bases):
        for v in ('b', 'c'):
            yield base + v + '.ogg', 'voice_variant_letter'
            yield base + v + '.ogg.sli', 'voice_variant_letter'
    # 3) loop 語音：已知 loop 編號 × 角色前綴（有界）
    for pre in sorted(prefixes):
        if not pre:
            continue
        for token in sorted(loop_tokens):
            yield '%s_loop_%s.ogg' % (pre, token), 'voice_loop_cross'
            yield '%s_loop_%s.ogg.sli' % (pre, token), 'voice_loop_cross'


def rule_evidence(rule_id: str) -> str:
    for rid, evidence in RULES:
        if rid == rule_id:
            return evidence
    return ''
