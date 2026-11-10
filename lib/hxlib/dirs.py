"""dirs.py -- 還原 Hxv4 對照表裡的「明文目錄」。

## 為什麼有這個模組

XP3 的 entry 只有「假檔名」；Hxv4 對照表其實是**按目錄分組**的：

    [domain_key, [ [file_hash, packed/key], ... ], domain_key, [...], ...]

`domain_key` 是 8 bytes（16 hex），社群清單裡的目錄 entry（`BC6792F8…:system/`）
用的就是這個 key。同一個 domain 的檔案＝原 build 裡同一個資料夾的檔案。

## 目錄名稱怎麼推出來（不用社群清單）

1. **分組**：從 Hxv4 表直接取 `domain_hash → [file_hash]`；root domain（每個
   archive 都有的那個）不算目錄。
2. **排序**：實測（tenshi data.xp3、lllj data.xp3）group 在表中的順序就是
   **目錄名稱的 ASCII 升冪排序**（`bgimage/ < bgm/ < font/ < k2compat/ <
   locale/cn/ < … < uipsd/func/ < video/`）。所以只要知道「有哪些目錄名」，
   排序就能把它們對到正確的 group。
3. **目錄名候選**：由 group 內已命中的檔名歸納（每個目錄的內容有明確特徵）：
   `k2compat_*.tjs` → `k2compat/`、`bgm*.opus` → `bgm/`、`*.ks.scn` → `scn/`、
   `map##.png` → `rule/`、`*.tft/*.otf` → `font/`、`sd###.mtn` → `sdmotion/`、
   `*.func` → `uipsd/func/`、`*.pbd/__pack.tlg` → `uipsd/`、語言字尾
   `_cn/_tw/_en` → `locale/<lang>/` …。這些都是「檔案內容/命名慣例」層級的證據。
4. **驗證**：把候選名排序後依序對到 group，再檢查每個 group 的內容是否符合被
   分到的目錄特徵（互相驗證）。只有全部一致才輸出；否則降級成部分輸出並在
   報告裡列出未對應的 group。

目錄名稱本身無法用 hash 驗證（8-byte key 的算法未知），所以這裡的產物標記為
''dir''（低風險、可審核）；key 直接取自 Hxv4 表，不是猜的。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

# 語言資料夾（引擎依語系挑檔；locale/cn 等）
LANG_DIRS = ('cn', 'tw', 'en', 'jp')

# 目錄內容特徵：回傳 True 表示這個 group 的內容「像」該目錄
# 證據：每個 predicate 都對應引擎的目錄用途（腳本/資料/資產慣例）
_TEXT_EXT = ('.tjs', '.ks', '.csv', '.txt', '.ini', '.toml', '.json', '.func')
_IMAGE_EXT = ('.png', '.jpg', '.tlg', '.pimg', '.webp', '.bmp')
_AUDIO_EXT = ('.ogg', '.opus', '.wav', '.mp3')
_MOVIE_EXT = ('.mp4', '.wmv', '.mtn', '.asd')


def _frac(names, pred):
    if not names:
        return 0.0
    return sum(1 for n in names if pred(n)) / len(names)


def _low(n):
    return n.lower()


def _starts(*prefixes):
    return lambda n: _low(n).startswith(prefixes)


def _ends(*exts):
    return lambda n: _low(n).endswith(exts)


def _is_one_of(*values):
    return lambda n: _low(n) in values


# 名稱 → (suffix, predicate, evidence)
DIR_CLASSIFIERS = (
    # 縮圖家族：檔名前綴本身就是子目錄名（bgthum/chthum/savethum/…）
    ('thum/bgthum/',
     lambda ns: _frac(ns, _starts('bgthum_')) >= 0.5,
     'thum/bgthum：背景縮圖 bgthum_*（ExChView 背景一覽）'),
    ('thum/chthum/',
     lambda ns: _frac(ns, _starts('chthum_')) >= 0.5,
     'thum/chthum：角色縮圖 chthum_*（_chthum_index.pbd 的角色縮圖表）'),
    ('thum/savethum/',
     lambda ns: _frac(ns, _starts('savethum_')) >= 0.5,
     'thum/savethum：存檔縮圖 savethum_*（savelist.csv 的 thumb）'),
    ('thum/movthum/',
     lambda ns: _frac(ns, _starts('thum_ed_', 'thum_op')) >= 0.4,
     'thum/movthum：影片縮圖 thum_ed_*/thum_op'),
    ('thum/cgthum/',
     lambda ns: _frac(ns, lambda n: _low(n).startswith('thum_')
                      and _low(n).endswith('.psb')) >= 0.5,
     'thum/cgthum：CG 縮圖資料 thum_*.psb'),
    ('thum/',
     lambda ns: _frac(ns, lambda n: _low(n).startswith('thum_')
                      and _low(n).endswith(_IMAGE_EXT)) >= 0.5,
     'thum：一般縮圖 thum_*.png（System.exePath+"data/thum/"）'),
    ('func/',
     lambda ns: _frac(ns, _ends('.func')) >= 0.4,
     'func：UI 函式 .func（tenshi 嵌在 uipsd/func/，lllj 在 archive 根）'),
    ('k2compat/',
     lambda ns: _frac(ns, _starts('k2compat')) >= 0.6,
     'k2compat.tjs 的 require() 載入 k2compat_*.tjs（TJS 原始碼證據）'),
    ('font/',
     lambda ns: (_frac(ns, _ends('.tft', '.otf', '.ttf')) >= 0.15
                 or any(_low(n) in ('embfontlist.tjs', 'deffontmap.tjs',
                                    'ctxfontprefs.tjs', 'ctxfontprefs.toml')
                        for n in ns)),
     '字型檔（tft/otf/ttf）與字型設定腳本'),
    ('bgm/',
     lambda ns: _frac(ns, lambda n: _low(n).startswith('bgm') and
                      _low(n).endswith(('.opus', '.ogg', '.sli'))) >= 0.5,
     'soundlist.csv 的 BGMxx -> bgmxx.opus'),
    ('bgimage/',
     lambda ns: any(_low(n) == 'base.stage' for n in ns),
     'bgimage：base.stage 舞台表（背景圖群組的唯一可靠特徵）'),
    ('sound/',
     lambda ns: _frac(ns, _ends('.ogg')) >= 0.6,
     'sound：效果音 .ogg（sysse.ini / PSB 的 SE 名）'),
    ('rule/',
     lambda ns: _frac(ns, lambda n: re.match(r'^map\d{2,3}[a-z0-9]*\.png$',
                                             _low(n)) is not None) >= 0.15
     or any(_low(n).startswith('rule_') for n in ns),
     'rule：地圖/規則圖 map##.png / rule_*.png'),
    ('sdmotion/',
     lambda ns: _frac(ns, lambda n: re.match(r'^sd\d{3}\.mtn$', _low(n))
                      is not None) >= 0.5,
     'sdmotion：SD 動作 sd###.mtn'),
    ('motion/',
     lambda ns: any(_low(n) in ('splash.mtn', 'yuzulogo.mtn', 'm2logo.mtn',
                                'title_bg.mtn', 'yuzusourlogo.mtn') for n in ns),
     'motion：標題/logo .mtn'),
    ('video/',
     lambda ns: _frac(ns, _ends('.mp4', '.wmv')) >= 0.5,
     'video：OP/ED 影片'),
    ('scn/',
     lambda ns: _frac(ns, _ends('.scn')) >= 0.6,
     'scn：編譯後場景 .ks.scn/.txt.scn'),
    ('scenario/transitions/',
     lambda ns: _frac(ns, lambda n: _low(n).startswith('trans_') and
                      _low(n).endswith('.tjs')) >= 0.5,
     'scenario/transitions：轉場腳本 trans_*.tjs（KAGLoadScript）'),
    ('scenario/scripts/',
     lambda ns: _frac(ns, lambda n: _low(n).startswith('script_') and
                      _low(n).endswith('.tjs')) >= 0.5,
     'scenario/scripts：script_*.tjs（script_kirie/lightedge/mosaic）'),
    ('others/krmovie_h264/',
     lambda ns: any(_low(n) in ('krmovie.dll',) for n in ns),
     'others/krmovie_h264：krmovie.dll（TJS 明確路徑 '
     'System.exePath+"others.xp3>krmovie_h264/"）'),
    ('uipsd/func/',
     lambda ns: _frac(ns, _ends('.func')) >= 0.4,
     'uipsd/func：UI 函式 .func'),
    ('uipsd/',
     lambda ns: _frac(ns, lambda n: _low(n).endswith(('.pbd', '__pack.tlg',
                                                     '__bg0.tlg'))) >= 0.4,
     'uipsd：UI 定義 .pbd + __pack/__bg0.tlg'),
    ('main/',
     lambda ns: any(_low(n) in ('config.tjs', 'cglist.csv', 'charvoice.csv',
                                'custom.ks', 'custom.tjs', 'first.ks') for n in ns),
     'main：config.tjs / cglist.csv / charvoice.csv 等主資料'
     '（flow_texts 路徑 System.exePath+"data/main/"）'),
    ('scenario/',
     lambda ns: any(_low(n) in ('!scnlist.txt', '_scnheads.txt', 'replay.ks',
                                'macro.ks', 'parsermacro.ks') for n in ns),
     'scenario：!scnlist.txt / replay.ks / macro.ks'),
    ('sysscn/',
     lambda ns: any(_low(n) in ('about.ks', 'advancedmenu.tjs', 'afterinit.tjs',
                                'backlog.tjs', 'cgmode.tjs') for n in ns),
     'sysscn：KAG 系統場景腳本（about.ks/advancedmenu.tjs/…）'),
    ('system/',
     lambda ns: any(_low(n) in ('action.tjs', 'affinelayer.tjs', 'storages.tjs',
                                'initialize.tjs', 'affinesource.tjs') for n in ns),
     'system：引擎核心腳本；startup.tjs 以 Scripts.execStorage("system/Initialize.tjs") '
     '載入，mounted 後存為 initialize.tjs'),
)

# 引擎/資料慣例下的候選目錄（沒有 group 的父目錄不列入）。
# 這些名字來自引擎的目錄慣例（initialize.tjs 的 autoPath 清單、
# System.exePath+"scenario/"、以及 .ks/.tjs/.mtn/.func 的副檔名語意）；
# 只用來在「group 順序 = 目錄名排序」的約束下填空，最後仍要通過內容驗證。
EXTRA_DIR_CANDIDATES = (
    'scenario/transitions/',   # trans_*.tjs = KAG 轉場模組
    'scenario/scripts/',       # script_*.tjs = 場景解析模組
    'sysscn/',                 # initialize.tjs addArchive("sysscn")
    'sdmotion/',               # sd###.mtn = SD 動作
    'locale/', 'others/',      # 父目錄（通常沒有自己的 group）
)


# locale/<lang>/ 的判斷：檔名語言字尾
_LOCALE_RE = re.compile(r'_(cn|tw|en|jp)(\.[A-Za-z0-9]+)$', re.IGNORECASE)


def classify_group_all(names) -> list:
    """回傳所有命中的 (目錄名, 證據)（依分類器順序）。"""
    known = [n for n in names if n and n != '?']
    if not known:
        return []
    out = []
    for dirname, pred, evidence in DIR_CLASSIFIERS:
        try:
            if pred(known):
                out.append((dirname, evidence))
        except Exception:
            continue
    return out


def _locale_suffix_stats(names) -> Counter:
    c = Counter()
    for n in names:
        m = _LOCALE_RE.search(n)
        if m:
            c[m.group(1).lower()] += 1
    return c


def _dir_validation_score(dirname, names) -> float:
    """內容與目錄特徵吻合度：1=完全吻合、0=不符、0.5=無資料。"""
    if not names:
        return 0.5
    m = re.match(r'^locale/(cn|tw|en|jp)/$', dirname)
    if m:
        lang = m.group(1)
        st = _locale_suffix_stats(names)
        if lang == 'jp':
            # jp = 預設語系：檔名不帶 cn/tw/en 字尾；帶 _jp 的也算（明確標記）
            foreign = sum(v for k, v in st.items() if k in ('cn', 'tw', 'en'))
            return 1.0 if foreign / max(1, len(names)) <= 0.1 else 0.3
        return 1.0 if st.get(lang, 0) >= max(1, sum(st.values()) * 0.5) else 0.3
    for d, pred, _ev in DIR_CLASSIFIERS:
        if d != dirname:
            continue
        try:
            return 1.0 if pred(names) else 0.0
        except Exception:
            return 0.0
    return 0.5


def derive_dirs(corpus, name_of, engine_dirs=()) -> dict:
    """還原 Hxv4 domain group → 目錄名。

    name_of: file_hash(大寫 hex) -> 已還原檔名；engine_dirs: TJS 邏輯取得的目錄候選。
    作法：每個 group 用內容特徵得到候選目錄；Hxv4 表中的 group 順序 == 目錄名
    ASCII 升冪（兩款遊戲都實測成立），用這個結構證據做 DFS 對齊，取內容驗證分數
    最高者；分數 < 0.8 時只輸出唯一候選並列出問題。
    """
    root = corpus.root_domain
    groups = []
    for (arc, dom), hashes in corpus.domain_files.items():
        if dom == root:
            continue
        names = [name_of.get(h.upper()) for h in hashes]
        names = [n for n in names if n]
        groups.append({'archive': arc, 'domain': dom, 'files': len(hashes),
                       'names': names})
    merged = {}
    order = []
    for g in groups:
        dom = g['domain']
        if dom not in merged:
            merged[dom] = {'domain': dom, 'archives': [], 'files': 0,
                           'names': [], 'candidates': [], 'evidence': ''}
            order.append(dom)
        m = merged[dom]
        m['archives'].append(g['archive'])
        m['files'] += g['files']
        m['names'].extend(g['names'])
    result_groups = [merged[d] for d in order]

    for g in result_groups:
        cands = classify_group_all(g['names'])
        names_only = [c[0] for c in cands]
        st = _locale_suffix_stats(g['names'])
        total = max(1, len(g['names']))
        strong = [c for c in names_only
                  if c not in ('bgimage/', 'font/') or
                  c in ('sysscn/', 'main/', 'uipsd/', 'rule/')]
        lang = st.most_common(1)[0][0] if st else None
        if lang and st[lang] / total >= 0.5:
            nm = 'locale/%s/' % lang
            g['candidates'] = [nm] + [c for c in names_only if c != nm]
            g['evidence'] = ('locale/%s：檔名帶 _%s 字尾' % (lang, lang))
        elif names_only:
            g['candidates'] = names_only
            g['evidence'] = cands[0][1]

    if any(g['candidates'] and g['candidates'][0].startswith('locale/')
           for g in result_groups):
        stems = set()
        for g in result_groups:
            for n in g['names']:
                m = _LOCALE_RE.search(n)
                if m:
                    stems.add(n[:m.start()] + m.group(2))
        for g in result_groups:
            hit = sum(1 for n in g['names'] if n in stems)
            if g['names'] and hit >= max(2, len(g['names']) * 0.5):
                strong = [c for c in g['candidates']
                          if c.split('/')[0] in ('font', 'system', 'sound', 'system')]
                if strong:
                    continue
                g['candidates'] = ['locale/jp/'] + [
                    c for c in g['candidates'] if c != 'locale/jp/']
                g['evidence'] = ('locale：檔名與 locale 語系檔同主幹但無語言字尾'
                                 ' = jp（預設語系）')

    # 候選順序微調：若 `<parent>/` 本身也是某個 group 的分類結果，則
    # `<parent>/<child>/` 這種巢狀候選優先（例如 func/ vs uipsd/func/）。
    all_simple = {g['candidates'][0] for g in result_groups
                  if len(g['candidates']) == 1}
    arch_stems = {a.stem for a in getattr(corpus, 'archives', [])}

    def _rank(d):
        # 若上層目錄剛好是一個獨立 .xp3（autoPath 掛載），代表它不太可能出現在
        # 這個 archive 的目錄群組裡（例如 lllj 有 uipsd.xp3 -> 用 func/ 而非
        # uipsd/func/；tenshi 沒有 uipsd.xp3 -> 用 uipsd/func/）。
        penalty = 0
        parts = d.rstrip('/').split('/')
        for k in range(1, len(parts)):
            if parts[k - 1] in arch_stems:
                penalty += 1
        return (penalty, -d.count('/'),
                0 if (d.rstrip('/').rpartition('/')[0] + '/') in all_simple else 1, d)

    for g in result_groups:
        if len(g['candidates']) > 1:
            g['candidates'] = sorted(g['candidates'], key=_rank)

    universe = set(engine_dirs)
    universe.update(EXTRA_DIR_CANDIDATES)
    for g in result_groups:
        universe.update(g['candidates'])
    for lang in LANG_DIRS:
        universe.add('locale/%s/' % lang)
    universe = {d if d.endswith('/') else d + '/' for d in universe
                if d and '//' not in d and not d.startswith('/') and
                all(ch not in d for ch in '\\:*?"<>|')}
    U = sorted(universe)

    domains = [g['domain'] for g in result_groups]
    n = len(domains)
    if n == 0:
        return {'map': {}, 'groups': result_groups, 'problems': [],
                'score': 1.0, 'candidates': U}

    def allowed(i, low, high):
        g = result_groups[i]
        pool = g['candidates'] if g['candidates'] else U
        return sorted(d for d in pool
                      if d > low and (high is None or d < high))

    # 後面上界：只用「唯一候選」的 group 當錨點（多候選 group 不能拿來當上界）
    upper = [None] * n
    cur = None
    for i in range(n - 1, -1, -1):
        upper[i] = cur
        c = result_groups[i]['candidates']
        if len(c) == 1:
            m = c[0]
            cur = m if cur is None else min(cur, m)

    solutions = []

    def dfs(i, last, chosen):
        if len(solutions) >= 200:
            return
        if i == n:
            score = sum(_dir_validation_score(chosen[d],
                                              result_groups[j]['names'])
                        for j, d in enumerate(domains)) / n
            solutions.append((score, dict(chosen)))
            return
        for name in allowed(i, last, upper[i]):
            chosen[domains[i]] = name
            dfs(i + 1, name, chosen)
            del chosen[domains[i]]

    dfs(0, '', {})
    if not solutions:
        # 沒有遞增解（有些遊戲的 group 順序不是目錄排序）：退回「每個 group
        # 取第一個未被用掉的候選」，再用內容驗證分數決定是否採用。
        used = set()
        greedy = {}
        order_idx = {g['domain']: i for i, g in enumerate(result_groups)}
        # 先處理候選集合小的（資訊多），再處理候選多的
        for g in sorted(result_groups, key=lambda g: (len(g['candidates']) == 0,
                                                      len(g['candidates']))):
            pick = None
            for cand in g['candidates']:
                if cand not in used:
                    pick = cand
                    break
            if pick is not None:
                used.add(pick)
                greedy[g['domain']] = pick
        # 沒有候選的 group：用剩下「父目錄已被使用」的引擎慣例目錄補
        leftovers = []
        for cand in EXTRA_DIR_CANDIDATES:
            if cand in used:
                continue
            parent = cand.rstrip('/').rpartition('/')[0] + '/'
            if parent and parent in used:
                leftovers.append(cand)
        leftovers.sort()
        for g in result_groups:
            if g['domain'] in greedy or not leftovers:
                continue
            greedy[g['domain']] = leftovers.pop(0)
        if greedy:
            gscore = sum(_dir_validation_score(greedy[d],
                                               result_groups[j]['names'])
                         for j, d in enumerate(domains) if d in greedy) / max(len(greedy), 1)
            if gscore >= 0.8 and len(greedy) == n:
                return {'map': {d.upper(): v for d, v in greedy.items()},
                        'groups': result_groups, 'problems': [],
                        'score': round(gscore, 3), 'candidates': U}
        partial = {g['domain']: g['candidates'][0]
                   for g in result_groups if len(g['candidates']) == 1}
        return {'map': {d.upper(): v for d, v in partial.items()},
                'groups': result_groups,
                'problems': [{'domain': d, 'reason': '無法排出遞增目錄序列'}
                             for d in domains if d not in partial],
                'score': 0.0, 'candidates': U}

    solutions.sort(key=lambda kv: -kv[0])
    score, assigned = solutions[0]
    problems = []
    if score < 0.8:
        partial = {g['domain']: g['candidates'][0]
                   for g in result_groups if len(g['candidates']) == 1}
        for g in result_groups:
            if g['domain'] not in partial:
                problems.append({'domain': g['domain'], 'reason': '信心不足',
                                 'known': len(g['names']), 'files': g['files']})
        assigned = partial
    else:
        for i, d in enumerate(domains):
            sc = _dir_validation_score(assigned[d], result_groups[i]['names'])
            if sc < 1.0 and result_groups[i]['names']:
                problems.append({'domain': d, 'assigned': assigned[d],
                                 'reason': '內容特徵與目錄不符', 'score': sc})
    return {'map': {d.upper(): v for d, v in assigned.items()},
            'groups': result_groups, 'problems': problems,
            'score': round(score, 3), 'candidates': U}
