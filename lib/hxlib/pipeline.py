"""pipeline.py -- Hxv4 檔名推導主流程。

    corpus（解密快取 + hash 表）
        │
        ├─ parser 模組（textfmt / psb / pbd / tjs / logic）
        │     └─ 每個模組回報「遊戲資料裡實際存在的字串」= Fact（含 context）
        │
        ├─ rules.expand_fragment()：依 context 補上引擎會加的字尾
        ├─ rules.derive_from_name()：已命中的名字再衍生 sidecar / pack / 語系兄弟檔
        │
        └─ verify.Verifier：BLAKE2s 對表，只保留真命中（含 provenance）

輸出：
    * GARbro 的 ``HxNames.lst``
    * ``work/report/<game>.json``：coverage、逐規則貢獻、殘留清單
    * ``work/report/<game>.md``：人看的版本
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from . import rules as R
from .corpus import (Corpus, CorpusEntry, LOGIC_KINDS, T_PSB, T_TEXT, T_TJS,
                     T_TJS_NS, T_PBD)
from .facts import Fact, KIND_DIR, KIND_FRAGMENT, KIND_NAME
from .progress import Reporter, fmt_seconds
from .verify import HashTable, Verifier, read_lst, write_lst

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


# ---------------------------------------------------------------- data holder
@dataclass
class Candidate:
    name: str
    rule: str
    source: str = ''


class Pipeline:
    def __init__(self, game_id: str, game_dir: str | Path, drip_path: str | Path,
                 *, work: str | Path | None = None, out: str | Path | None = None,
                 corpus_root: str | Path | None = None, reuse_corpus: bool = True,
                 verbose: bool = True,
                 workers: int | None = None, extra_fact_globs: Iterable[str] = (),
                 log_file: str | Path | None = None):
        self.game_id = game_id
        self.game_dir = Path(game_dir)
        self.drip_path = Path(drip_path)
        self.work = Path(work) if work else (ROOT / 'work')
        self.out = Path(out) if out else (self.work / ('HxNames.%s.lst' % game_id))
        self.corpus_root = Path(corpus_root) if corpus_root else (self.work / 'corpus')
        self.reuse_corpus = reuse_corpus
        self.verbose = verbose
        self.workers = workers or max(1, min(8, (os.cpu_count() or 4) - 1))
        self.reporter = Reporter(log_file, verbose=verbose)
        self._stage_i = 0
        self._stage_total = 13
        self.extra_fact_globs = list(extra_fact_globs)

        self.corpus: Corpus | None = None
        self.table: HashTable | None = None
        self.verifier: Verifier | None = None
        self.found: dict[str, str] = {}          # digest hex -> name
        self.prov: dict[str, dict] = {}          # digest hex -> {'rule','source'}
        self.rule_hits: dict[str, int] = {}
        self.module_hits: dict[str, int] = {}
        self.stats: dict[str, object] = {}

        self._buf: list[Candidate] = []
        self._buf_sources: dict[str, int] = {}
        self.dir_map: dict[str, str] = {}       # 16-hex domain key -> 目錄名
        self._stage_pending: list[tuple[Fact, str, str]] = []
        self._stage_times: list[str] = []
        self._stage_seasons: list[str] = []
        self._stand_bases: set[str] = set()
        self._engine_dirs: set[str] = set()
        self._stem_pool: set[str] = set()
        self._templates: set[str] = set()
        self._char_pool: set[str] = set()
        self._token_pool: set[str] = set()

    # ------------------------------------------------------------- logging
    def log(self, *a) -> None:
        self.reporter.log(*a)

    def progress(self, *a, **kw) -> None:
        self.reporter.progress(*a, **kw)

    def elapsed(self, t0: float) -> str:
        return '%.1fs' % (time.time() - t0)

    def _verify_progress(self, probed: int, hits: int) -> None:
        self.progress('hash 驗證', probed, extra='hits=%d' % hits)

    # ------------------------------------------------------------ facts cache
    # 解析（parser / logic）結果的快取：同一份 corpus manifest 只算一次。
    CACHE_VERSION = 4

    def _cache_path(self, kind: str) -> Path:
        return self.work / 'cache' / ('facts_%s_%s_v%d.jsonl'
                                      % (kind, self.game_id, self.CACHE_VERSION))

    def _cache_fingerprint(self, kind: str) -> str:
        import hashlib
        h = hashlib.md5()
        h.update(('%s|%s|%d' % (self.game_id, kind,
                                self.CACHE_VERSION)).encode())
        try:
            man = self.corpus._manifest_path() if self.corpus else None
        except Exception:
            man = None
        if man is not None and Path(man).is_file():
            st = Path(man).stat()
            h.update(('%d:%d' % (st.st_mtime_ns, st.st_size)).encode())
        return h.hexdigest()

    def _cache_open(self, kind: str):
        path = self._cache_path(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        return path, tmp, tmp.open('w', encoding='utf-8')

    def _cache_close(self, kind: str, path: Path, tmp, fh) -> None:
        fh.close()
        tmp.replace(path)
        (path.with_suffix('.fp')).write_text(self._cache_fingerprint(kind),
                                             encoding='utf-8')

    def _cache_write(self, fh, modname: str, src: str, facts) -> None:
        if fh is None:
            return
        g = '%s|%s' % (modname, src)
        for f in facts:
            fh.write(json.dumps({'g': g,
                                 'f': [f.value, f.kind, f.source,
                                       f.note, f.context]},
                                ensure_ascii=False) + '\n')

    def _load_facts_cache(self, kind: str) -> bool:
        path = self._cache_path(kind)
        fp = path.with_suffix('.fp')
        if not path.is_file() or not fp.is_file():
            return False
        try:
            if fp.read_text(encoding='utf-8').strip() != self._cache_fingerprint(kind):
                return False
        except OSError:
            return False
        groups: dict[str, list[Fact]] = {}
        with path.open(encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    v = r['f']
                    fact = Fact(value=v[0], kind=v[1], source=v[2],
                                note=v[3], context=v[4])
                except Exception:
                    continue
                groups.setdefault(r['g'], []).append(fact)
        for g, facts in groups.items():
            modname, _, src = g.partition('|')
            self._handle_facts(facts, modname, src)
        self.stats['cache_' + kind] = len(groups)
        return True

    # ------------------------------------------------------ verified-found cache
    def _found_cache_path(self, kind: str) -> Path:
        return self.work / 'cache' / ('found_%s_%s_v%d.json'
                                      % (kind, self.game_id, self.CACHE_VERSION))

    def _code_stamp(self) -> str:
        import hashlib
        h = hashlib.md5()
        for name in ('rules.py', 'logic.py', 'grammar.py', 'pipeline.py',
                     'corpus.py', 'verify.py'):
            p = HERE / name
            try:
                st = p.stat()
                h.update(('%s:%d:%d' % (name, st.st_mtime_ns, st.st_size)).encode())
            except OSError:
                pass
        return h.hexdigest()

    def _save_found_cache(self, kind: str) -> None:
        path = self._found_cache_path(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        obj = {
            'fingerprint': self._cache_fingerprint(kind) + '|' + self._code_stamp(),
            'found': self.found,
            'prov': self.prov,
            'rule_hits': self.rule_hits,
            'module_hits': self.module_hits,
            'engine_dirs': sorted(self._engine_dirs),
            'stand_bases': sorted(self._stand_bases),
            'stage_times': self._stage_times,
            'stage_seasons': self._stage_seasons,
            'templates': sorted(self._templates),
            'char_pool': sorted(self._char_pool),
            'stem_pool': sorted(self._stem_pool),
            'token_pool': sorted(self._token_pool),
        }
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)

    def _load_found_cache(self, kind: str) -> bool:
        path = self._found_cache_path(kind)
        if not path.is_file():
            return False
        try:
            obj = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return False
        if obj.get('fingerprint') != (self._cache_fingerprint(kind) + '|'
                                      + self._code_stamp()):
            return False
        found = obj.get('found') or {}
        self.found.update(found)
        self.prov.update(obj.get('prov') or {})
        for k, v in (obj.get('rule_hits') or {}).items():
            self.rule_hits[k] = self.rule_hits.get(k, 0) + v
        for k, v in (obj.get('module_hits') or {}).items():
            self.module_hits[k] = self.module_hits.get(k, 0) + v
        self._engine_dirs.update(obj.get('engine_dirs') or [])
        self._stand_bases.update(obj.get('stand_bases') or [])
        self._stage_times = list(obj.get('stage_times') or [])
        self._stage_seasons = list(obj.get('stage_seasons') or [])
        self._templates.update(obj.get('templates') or [])
        self._char_pool.update(obj.get('char_pool') or [])
        self._stem_pool.update(obj.get('stem_pool') or [])
        self._token_pool.update(obj.get('token_pool') or [])
        self.stats['found_cache_' + kind] = len(found)
        return True

    # ----------------------------------------------------------------- setup
    def load(self) -> None:
        t0 = time.time()
        self.corpus = Corpus(self.game_id, self.game_dir, self.drip_path,
                             self.corpus_root)
        self.corpus.verbose = self.verbose
        self.corpus.log_fn = self.reporter.log
        self.corpus.scan()
        extract_workers = self.workers or max(
            1, min(8, (os.cpu_count() or 4) - 1))
        self.corpus.extract(reuse=self.reuse_corpus, workers=extract_workers)
        self.table = HashTable.from_corpus(self.corpus)
        self.verifier = Verifier(self.table, workers=self.workers,
                                 verbose=self.verbose)
        self.verifier.progress_cb = self._verify_progress
        if hasattr(self.verifier, 'log'):
            self.verifier.log_fn = self.reporter.log
        self.log('  verifier: %d hashes, %d workers' % (
            len(self.table), self.verifier.workers))
        # index 明文檔名（例如 startup.tjs）直接算命中
        for h, (name, _arc, _idx) in self.corpus.plain.items():
            hh = h.upper()
            if hh not in self.found:
                self.found[hh] = name
                self.prov[hh] = {'rule': 'index_plaintext', 'source': 'xp3 index'}
                self.rule_hits['index_plaintext'] = self.rule_hits.get(
                    'index_plaintext', 0) + 1
        self.log('corpus ready: %s (%s)' % (self.corpus, self.elapsed(t0)))

    def close(self) -> None:
        if self.verifier:
            self.verifier.close()
        if self.corpus:
            self.corpus.close()
        self.reporter.close()

    def _run_stage(self, name: str, fn):
        self._stage_i += 1
        self.reporter.stage(self._stage_i, self._stage_total, name)
        t0 = time.time()
        out = fn()
        self.log('  [done] %s (%s)' % (name, self.elapsed(t0)))
        return out

    # ------------------------------------------------------------ parser set
    def _modules(self) -> list:
        """回傳可用的 parser 模組（缺檔就跳過，方便漸進整合）。"""
        mods = []
        from .formats import textfmt
        mods.append(textfmt)
        have = set()
        for name in ('psb', 'pbd', 'tjs'):
            try:
                mod = __import__('hxlib.formats.' + name, fromlist=['iter_facts'])
            except Exception as ex:
                self.log('  [modules] %s 未就緒（%s）' % (name, ex))
                continue
            if hasattr(mod, 'iter_facts'):
                mods.append(mod)
                have.add(name)
            else:
                self.log('  [modules] %s 沒有 iter_facts()' % name)
        if 'psb' not in have:
            # psb.py 不在時才用字串級的 psbraw（較慢、只做保險）
            try:
                from .formats import psbraw
                mods.append(psbraw)
                self.log('  [modules] 使用 psbraw 替代 psb（字串級抽取）')
            except Exception:
                pass
        else:
            # psb.py 只抽語意欄位；psbraw 補「所有字串葉子」（語音 id / SE 名 /
            # 縮圖名常常藏在任意 key 下，只靠語意欄位會漏）。
            try:
                from .formats import psbraw
                mods.append(psbraw)
            except Exception:
                pass
        # tjsraw：TJS 容器的所有字串常量（設定表的檔名 stem 常以純字串存在，
        # 執行時才接副檔名；engine_ext_fill 會逐一接副檔名再用 hash 驗證）。
        try:
            from .formats import tjsraw
            mods.append(tjsraw)
        except Exception:
            pass
        return mods

    # ------------------------------------------------------------- probing
    def probe(self, cand: Candidate) -> None:
        """把候選塞進 buffer，滿了就平行驗證。"""
        if not cand.name or len(cand.name) > 255:
            return
        self._buf.append(cand)
        if len(self._buf) >= 400_000:
            self.flush()

    def flush(self) -> int:
        if not self._buf:
            return 0
        buf, self._buf = self._buf, []
        # 以 name 去重（同一名字只驗一次，保留第一個 provenance）
        names: list[str] = []
        meta: dict[str, Candidate] = {}
        for c in buf:
            if c.name not in meta:
                meta[c.name] = c
                names.append(c.name)
        hits = self.verifier.verify(names)
        added = 0
        for h, name in hits.items():
            if h in self.found:
                continue
            c = meta.get(name)
            self.found[h] = name
            if c is not None:
                self.prov[h] = {'rule': c.rule, 'source': c.source}
                self.rule_hits[c.rule] = self.rule_hits.get(c.rule, 0) + 1
                mod = _module_of(c.source)
                self.module_hits[mod] = self.module_hits.get(mod, 0) + 1
            else:
                self.prov[h] = {'rule': 'unknown', 'source': ''}
            added += 1
        return added

    def probe_fast(self, names, rule: str, source: str) -> tuple[int, int]:
        """高效候選驗證：直接把 name generator 交給 Verifier（跳過 Candidate 物件）。

        回傳 (probed, added)。命中者記在同一個 rule/source 下。
        """
        before = self.verifier.stats.get('probed', 0)

        def counted():
            n = 0
            for name in names:
                n += 1
                if n % 2_000_000 == 0:
                    self.progress('候選產生', n)
                yield name

        hits = self.verifier.verify(counted())
        probed = self.verifier.stats.get('probed', 0) - before
        added = 0
        for h, name in hits.items():
            if h in self.found:
                continue
            self.found[h] = name
            self.prov[h] = {'rule': rule, 'source': source}
            self.rule_hits[rule] = self.rule_hits.get(rule, 0) + 1
            mod = _module_of(source)
            self.module_hits[mod] = self.module_hits.get(mod, 0) + 1
            added += 1
        return probed, added

    # ------------------------------------------------------------- stages
    def _handle_facts(self, facts, modname: str, src: str) -> None:
        """把一批 Fact 轉成候選（直接 / 片段展開 / stage 樣板）。"""
        for f in facts:
            value = f.value or ''
            # 收集「引擎用過的路徑字首」當目錄候選（dirs.py 需要）
            if value.endswith('/'):
                self._add_engine_dir(value)
            elif '/' in value and not value.startswith('http'):
                idx = value.find('/')
                while idx >= 0:
                    self._add_engine_dir(value[:idx + 1])
                    idx = value.find('/', idx + 1)
            if f.kind in (KIND_NAME, KIND_DIR):
                # 模板字串也可能以 NAME 形式出現在 TJS stringpool
                v0 = f.value.strip()
                if (2 <= len(v0) <= 120 and '%' in v0 and ' ' not in v0
                        and any(t in v0 for t in ('%s', '%d', '%08x',
                                                  '%08X'))):
                    self._templates.add(v0)
                for tok in re.findall(r'[A-Za-z][A-Za-z0-9]{1,11}', v0):
                    t = tok.lower()
                    if t.isascii() and t.isalnum():
                        self._token_pool.add(t)
                self.probe(Candidate(f.value, 'direct', src))
                # XP3 entry 名稱一律小寫（引擎 convention）；資料檔常寫成
                # 大小寫混合（Kosugi-Regular.ttf），故同時試小寫變體。
                low = f.value.lower()
                if low != f.value:
                    self.probe(Candidate(low, 'direct-lower', src))
                continue
            if f.kind != KIND_FRAGMENT:
                continue
            # 檔名 stem 池：FRAGMENT 常常是「執行時再接副檔名」的檔名主體
            # （SE 名、設定表 stem…）。之後在 family stage 統一接上引擎實際
            # 出現過的副檔名再用 hash 驗證——跨遊戲通用。
            stem = f.value.strip()
            # KAG storage 語法 `file|label`：真檔名只取 `|` 之前
            if '|' in stem:
                stem = stem.split('|', 1)[0].strip()
            if (2 <= len(stem) <= 80
                    and not any(c in stem for c in '\\/:*?"<>|\r\n\t')
                    and not any(t in stem for t in ('${', '%d', '%s', 'TIME',
                                                    'SEASON'))
                    and '.' not in stem):
                self._stem_pool.add(stem)
            ctx = f.context or ''
            # 收集「名字模板」（%s / %08x / %d）與「角色名池」：前者是引擎
            # 產生 runtime 名字的格式字串（例如 "%s%08x%08x.png"、'chthum_%s_'），
            # 後者是 CSV 角色表第一欄。兩者都是資料驅動，不寫死遊戲常數。
            if f.kind == KIND_FRAGMENT:
                v = f.value.strip()
                if (2 <= len(v) <= 120 and '%' in v and ' ' not in v
                        and any(t in v for t in ('%s', '%d', '%08x', '%08X'))):
                    self._templates.add(v)
                if (':csv:col0' in ctx
                        and any(k in ctx.lower() for k in
                                ('char', 'scn', 'kag', 'chart'))):
                    if 1 <= len(v) <= 24 and not any(
                            c in v for c in '\r\n\t'):
                        self._char_pool.add(v)
                for tok in re.findall(r'[A-Za-z][A-Za-z0-9]{1,11}', v):
                    t = tok.lower()
                    if t.isascii() and t.isalnum():
                        self._token_pool.add(t)
            elif (f.kind == KIND_NAME and ':csv:col0' in ctx
                  and any(k in ctx.lower() for k in
                          ('char', 'scn', 'kag', 'chart'))):
                v = f.value.strip()
                if 1 <= len(v) <= 24:
                    self._char_pool.add(v)
            if ctx == 'stage:prefix':
                self._add_stage_prefix(f.value)
                continue
            if ctx.startswith('stage:') and ('TIME' in f.value or 'SEASON' in f.value):
                self._stage_pending.append((f, src, f.source))
                continue
            if ctx in ('stand:filename', 'pbd:filename', 'pbd:layerFilename'):
                # 立繪 base（.stand 的 filename:'かぐ耶a'）→ 之後與 PBD layer id 組合
                if '.' not in f.value:
                    self._stand_bases.add(f.value)
            for name, rule in R.expand_fragment(f):
                self.probe(Candidate(name, rule, src))

    def gather_and_expand(self) -> None:
        """逐 entry 串流處理：parser → Fact → 直接候選 / 片段展開（結果有快取）。"""
        t0 = time.time()
        if self._load_found_cache('parsers'):
            self.log('  parsers found-cache hit -> %d found (%s)'
                     % (len(self.found), self.elapsed(t0)))
            return
        if self._load_facts_cache('parsers'):
            self.flush()
            self._save_found_cache('parsers')
            self.log('  parsers facts-cache hit -> %d found (%s)'
                     % (len(self.found), self.elapsed(t0)))
            return
        kinds = set(LOGIC_KINDS)
        items: list[dict] = []
        for ce in self.corpus.entries.values():
            if not ce.path or ce.kind not in kinds:
                continue
            items.append({'hash': ce.hash, 'archive': ce.archive,
                          'index': ce.index, 'kind': ce.kind,
                          'path': str(self.corpus.root / ce.path)})
        self.log('  parsers: %d files' % len(items))
        n_files = 0
        n_facts = 0
        cache_path, cache_tmp, cache_fh = self._cache_open('parsers')
        try:
            if self.workers and self.workers > 1 and len(items) > 200:
                chunk = 128
                chunks = [items[i:i + chunk]
                          for i in range(0, len(items), chunk)]
                self.log('  parsers: parallel %d workers, %d chunks'
                         % (self.workers, len(chunks)))
                results = _run_parse_tasks(
                    chunks, self.workers,
                    progress_cb=lambda dc, tc, df, tf: self.progress(
                        'parser 檔案', df, tf,
                        extra='chunks %d/%d' % (dc, tc)))
            else:
                mods = self._modules()
                results = []
                buf = []
                for ce, data in self.corpus.iter_files(kinds=kinds):
                    per_mod = []
                    for mod in mods:
                        mkinds = getattr(mod, 'KINDS', None)
                        if mkinds is not None and ce.kind not in mkinds:
                            continue
                        try:
                            got = mod.iter_facts(ce, data)
                        except Exception:
                            continue
                        if got:
                            modname = getattr(mod, '__name__', '?').rsplit(
                                '.', 1)[-1]
                            per_mod.append((modname, '%s:%s#%d'
                                            % (modname, ce.archive, ce.index),
                                            list(got)))
                    results.append(per_mod)
            for rows in results:
                for modname, src, facts in rows:
                    n_facts += len(facts)
                    n_files += 1
                    self._cache_write(cache_fh, modname, src, facts)
                    self._handle_facts(facts, modname, src)
                    if n_files % 3000 == 0:
                        self.log('  parsers: %d entries, %d facts, %d found (%s)'
                                 % (n_files, n_facts, len(self.found),
                                    self.elapsed(t0)))
        finally:
            self._cache_close('parsers', cache_path, cache_tmp, cache_fh)
        self.flush()
        self.stats['parser_files'] = len(items)
        self.stats['facts'] = n_facts
        self._save_found_cache('parsers')
        self.log('  parsers done: %d files, %d facts -> %d found (%s)'
                 % (len(items), n_facts, len(self.found), self.elapsed(t0)))

    def load_extra_facts(self) -> None:
        """載入 teammate/外部產生的補充 Fact JSONL（work/facts/tjsextra_<game>.jsonl）。"""
        from .facts import iter_jsonl
        d = self.work / 'facts'
        files = sorted(d.glob('tjsextra_%s.jsonl' % self.game_id)) if d.is_dir() else []
        n = 0
        for path in files:
            facts = list(iter_jsonl(path))
            n += len(facts)
            self._handle_facts(facts, 'tjsextra', 'tjsextra:%s' % path.name)
        # teammate 產生的「名稱清單」（每行一個名字，多半已用 hash 驗證過；
        # pipeline 仍會重新算 BLAKE2s，不在表裡的自然被丟掉）
        if d.is_dir():
            lists = sorted(set(d.glob('phase2_hits_%s*.lst' % self.game_id))
                           | set(d.glob('phase2_hits_*_%s.lst' % self.game_id))
                           | set(d.glob('phase2_hits2_%s.lst' % self.game_id)))
        else:
            lists = []
        for path in lists:
            for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
                s = line.strip()
                if not s or s.startswith('#'):
                    continue
                if ':' in s and len(s.split(':', 1)[0]) in (16, 64):
                    s = s.split(':', 1)[1]
                if not s:
                    continue
                n += 1
                self.probe(Candidate(s, 'phase2_hits', 'phase2:%s' % path.name))
        # 動態證據（引擎實際開過的檔名）：KrkrDump 產生的 HxNames.dynamic.lst。
        # 這是「引擎 oracle」而不是社群名單；沒有檔案的遊戲自然跳過。
        dyn_paths = [self.work / 'HxNames.dynamic.lst',
                     self.work / ('HxNames.dynamic.%s.lst' % self.game_id),
                     self.work / 'dynamic' / 'HxNames.dynamic.lst',
                     self.work / 'dyn' / self.game_id / 'HxNames.dynamic.lst',
                     Path(self.game_dir) / '_assets' / 'HxNames.dynamic.lst']
        dyn_n = 0
        for path in dyn_paths:
            if not path.is_file():
                continue
            for h, name in read_lst(path).items():
                if len(h) != 64:
                    continue
                dyn_n += 1
                self.probe(Candidate(name, 'dynamic-evidence',
                                     'dynamic:%s' % path.name))
        if dyn_n:
            self.flush()
            self.stats['dynamic_evidence'] = dyn_n
            self.log('  [dynamic] %d names from engine dump -> %d found'
                     % (dyn_n, len(self.found)))
        if lists:
            self.flush()
        if n:
            self.flush()
            self.stats['extra_facts'] = n
            self.log('  [extra] %d facts from %s -> %d found'
                     % (n, ', '.join(p.name for p in files), len(self.found)))

    def gather_logic(self) -> None:
        """TJS 原始碼層級的邏輯分析（需要全檔一起做常量傳播，所以獨立一階段）。"""
        t0 = time.time()
        if self._load_found_cache('logic'):
            self.log('  logic found-cache hit -> %d found (%s)'
                     % (len(self.found), self.elapsed(t0)))
            return
        if self._load_facts_cache('logic'):
            self.flush()
            self._save_found_cache('logic')
            self.log('  logic facts-cache hit -> %d found (%s)'
                     % (len(self.found), self.elapsed(t0)))
            return
        try:
            from .formats import tjs as tjsmod
            from . import logic
        except Exception as ex:
            self.log('  [logic] 未就緒（%s）' % ex)
            return
        entries = [(ce, data) for ce, data in self.corpus.iter_files(kinds=('tjs',))]
        if not entries:
            return
        try:
            rep = tjsmod.decompile_corpus(
                entries, self.game_id, root=str(self.work),
                log=self.reporter.log)
            self.stats['decompile'] = {'ok': rep.get('ok'), 'failed': rep.get('failed'),
                                       'skipped': rep.get('skipped')}
        except Exception as ex:
            self.log('  [logic] 反編譯失敗（%s: %s），跳過' % (type(ex).__name__, ex))
            return
        sources: dict[str, str] = {}
        for row in tjsmod.load_decompile_index(self.game_id, root=str(self.work)):
            if row.get('ok') and row.get('source_path'):
                try:
                    sources['%s#%s' % (row.get('archive'), row.get('index'))] = Path(
                        row['source_path']).read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue
        if not sources:
            return
        try:
            facts = list(logic.analyze_sources(sources))
        except Exception as ex:
            self.log('  [logic] 分析失敗（%s: %s），跳過' % (type(ex).__name__, ex))
            return
        self.stats['logic'] = logic.last_report()
        self.log('  [logic] %d scripts -> %d facts' % (len(sources), len(facts)))
        cache_path, cache_tmp, cache_fh = self._cache_open('logic')
        try:
            self._cache_write(cache_fh, 'logic', 'logic', facts)
        finally:
            self._cache_close('logic', cache_path, cache_tmp, cache_fh)
        self._handle_facts(facts, 'logic', 'logic')
        self.flush()
        self.stats['logic_facts'] = len(facts)
        self._save_found_cache('logic')
        self.log('  logic done -> %d found (%s)' % (len(self.found), self.elapsed(t0)))

    def _add_engine_dir(self, value: str) -> None:
        d = value if value.endswith('/') else value + '/'
        body = d.rstrip('/')
        if (body and '//' not in d and not d.startswith('/') and
                len(d) <= 80 and
                all(ch not in d for ch in '\\:*?"<>|(){};=[]%&')):
            self._engine_dirs.add(d)
            # System.exePath + "data/main/" 這種 exe 相對路徑，archive 內是 "main/"
            if d.startswith('data/') and len(d) > len('data/'):
                self._engine_dirs.add(d[len('data/'):])

    def _add_stage_prefix(self, value: str) -> None:
        if re.fullmatch(r'[A-Za-z]', value):
            if value not in self._stage_times:
                self._stage_times.append(value)
        elif value in ('春', '夏', '秋', '冬'):
            if value not in self._stage_seasons:
                self._stage_seasons.append(value)

    def _flush_stage_pending(self) -> None:
        pending, self._stage_pending = self._stage_pending, []
        if not pending:
            return
        times = self._stage_times or ['a']
        seasons = self._stage_seasons or ['春', '夏', '秋', '冬']
        for f, src, detail in pending:
            for t in times:
                base = f.value.replace('TIME', t)
                if 'SEASON' in base:
                    for s in ([''] + ['_' + x for x in seasons]):
                        cand = base.replace('SEASON', s)
                        for name, rule in R.expand_fragment(
                                Fact(cand, KIND_FRAGMENT, detail, '', f.context)):
                            self.probe(Candidate(name, rule, src))
                else:
                    for name, rule in R.expand_fragment(
                            Fact(base, KIND_FRAGMENT, detail, '', f.context)):
                        self.probe(Candidate(name, rule, src))
        self.flush()

    def derive(self) -> None:
        """由已命中的名字衍生兄弟檔（sidecar / pack / 語系），直到不再成長。"""
        t0 = time.time()
        rounds = 0
        while rounds < 6:
            rounds += 1
            before = len(self.found)
            for name in list(self.found.values()):
                for cand, rule in R.derive_from_name(name):
                    self.probe(Candidate(cand, rule, 'derived:' + name))
            self.flush()
            self.log('  derive round %d: %d (%+d, %s)'
                     % (rounds, len(self.found), len(self.found) - before,
                        self.elapsed(t0)))
            if len(self.found) == before:
                break
        self.stats['derive_rounds'] = rounds

    def derive_bounded(self) -> None:
        """引擎慣例的有界家族（例如 mapselect 的 map##.png）一次性探測。"""
        n = 0
        for name, rule in R.bounded_candidates():
            self.probe(Candidate(name, rule, 'bounded'))
            n += 1
        self.flush()
        self.stats['bounded_candidates'] = n
        self.log('  [bounded] %d candidates -> %d found' % (n, len(self.found)))

    def derive_data_tables(self) -> None:
        """由遊戲自己的資料檔組合名字（sysse.ini 的 sysvo 樣板、charvoice 前綴、
        embfontlist 的字型 atlas 樣板）。

        這些都是「執行時才組合」的名字：資料檔只存 `%s_attention0` 樣板 +
        角色前綴清單，實際檔名要在載入時展開。規則與證據見
        ``rules.DATA_TABLE_RULE_EVIDENCE``。
        """
        import re
        t0 = time.time()
        from .formats import textfmt
        sysvo_templates: set[str] = set()
        prefixes: set[str] = set()
        tft_templates: set[str] = set()
        sizes: set[int] = set()
        scanned = 0
        for ce, data in self.corpus.iter_files(kinds=('text',)):
            scanned += 1
            try:
                dec = textfmt.decode_text(data)
            except Exception:
                continue
            if not dec:
                continue
            text = dec[0]
            if 'sysvo.format' in text:
                for line in text.splitlines():
                    m = re.match(r'\s*sysvo\.format\.[A-Za-z0-9_.]+\s*=\s*(.+?)\s*$',
                                 line)
                    if not m:
                        continue
                    v = m.group(1).strip()
                    if not v or v.startswith('@') or '%s' not in v:
                        continue
                    if len(v) <= 80:
                        sysvo_templates.add(v)
            if '話者に対するボイス割り当て' in text:
                for line in text.splitlines():
                    cols = line.split(',')
                    if len(cols) < 2 or '%s' not in cols[1]:
                        continue
                    pre = cols[1].split('%')[0].rstrip('_')
                    if pre and re.fullmatch(r'[A-Za-z0-9_]{2,20}', pre):
                        prefixes.add(pre)
            if '.tft' in text and '"file"' in text:
                for m in re.finditer(r'"file"\s*=>\s*"([^"]+\.tft)"', text):
                    tft_templates.add(m.group(1))
                for m in re.finditer(
                        r'"size"\s*=>\s*\(const\)\s*\[([0-9,\s]+)\]', text):
                    for x in m.group(1).split(','):
                        x = x.strip()
                        if x.isdigit() and 1 <= int(x) <= 200:
                            sizes.add(int(x))
        n = 0
        if prefixes and sysvo_templates:
            for tmpl in sorted(sysvo_templates):
                for pre in sorted(prefixes):
                    self.probe(Candidate(tmpl.replace('%s', pre) + '.ogg',
                                         'ui_voice_sysvo', 'data-table'))
                    n += 1
            for pre in sorted(prefixes):
                self.probe(Candidate('chaticon_%s.png' % pre,
                                     'chaticon', 'data-table'))
                n += 1
        # CG 清單（cgview/savelist/imagediffmap）：內容列出所有 CG 變體名
        # （例 `thum_ev352,ev352ca,ev352ba,…`、`savethum_ev153m,ev153m*`）→
        # 由 token 衍生 thum_<cg>.png / savethum_<cg>.jpg。
        cg_tokens: set[str] = set()
        for ce, data in self.corpus.iter_files(kinds=('text',)):
            try:
                dec = textfmt.decode_text(data)
            except Exception:
                continue
            if not dec:
                continue
            text = dec[0]
            if ('savethum_' not in text and 'thum_ev' not in text
                    and not re.search(r'\bev\d{3}[a-z]{1,2}\b', text)):
                continue
            for tok in re.findall(r'[A-Za-z][A-Za-z0-9_]{2,24}', text):
                if re.search(r'\d', tok):
                    cg_tokens.add(tok)
        for tok in sorted(cg_tokens):
            for cand in ('thum_%s.png' % tok, 'savethum_%s.jpg' % tok):
                self.probe(Candidate(cand, 'cg_table', 'cg-listing'))
                n += 1
        self.stats['cg_tokens'] = len(cg_tokens)

        if tft_templates and sizes:
            for tmpl in sorted(tft_templates):
                if '${size}' in tmpl:
                    for s in sorted(sizes):
                        self.probe(Candidate(tmpl.replace('${size}', str(s)),
                                             'font_atlas', 'data-table'))
                        n += 1
                elif '%d' in tmpl:
                    for s in sorted(sizes):
                        self.probe(Candidate(tmpl.replace('%d', str(s)),
                                             'font_atlas', 'data-table'))
                        n += 1
        self.flush()
        self.stats['data_tables'] = {
            'text_entries': scanned, 'sysvo_templates': len(sysvo_templates),
            'char_prefixes': len(prefixes), 'tft_templates': len(tft_templates),
            'font_sizes': len(sizes), 'candidates': n,
        }
        self.log('  [data-tables] sysvo=%d prefixes=%d tft=%d sizes=%d '
                 'candidates=%d -> %d found (%s)'
                 % (len(sysvo_templates), len(prefixes), len(tft_templates),
                    len(sizes), n, len(self.found), self.elapsed(t0)))

    def gather_pbd_parts(self) -> None:
        """立繪圖層零件：`<stand base>_<layer_id>.tlg` / `<base>_0_<layer_id>.tlg`。

        base 來自 .stand 的 `filename:'…'`（gather 階段收集），layer id 來自
        .pbd 圖層清單（pbd2json 解析）。兩組資料都是遊戲自己的資料，交叉組合後
        由 hash 表驗證 —— 見 rules.STAND_LAYER_PARTS 的說明。
        """
        t0 = time.time()
        # 已命中的 .sinfo / .stand 檔名本身就是 base（かぐ耶a.sinfo -> かぐ耶a）
        for name in self.found.values():
            low = name.lower()
            if low.endswith(('.sinfo', '.stand')):
                base = name.rsplit('.', 1)[0]
                if base and '.' not in base:
                    self._stand_bases.add(base)
        if not self._stand_bases:
            return
        ids = self._pbd_layer_ids()
        if not ids:
            self.log('  [pbd] 沒有 layer id（pbd2json 不可用？）')
            return
        for base in sorted(self._stand_bases):
            for lid in ids:
                self.probe(Candidate('%s_%d.tlg' % (base, lid),
                                     R.STAND_LAYER_PARTS.id, 'pbd-parts'))
                self.probe(Candidate('%s_0_%d.tlg' % (base, lid),
                                     R.STAND_LAYER_PARTS.id, 'pbd-parts'))
        self.flush()
        self.stats['stand_bases'] = len(self._stand_bases)
        self.stats['pbd_layer_ids'] = len(ids)
        self.log('  [pbd] %d bases x %d layer ids -> %d found (%s)'
                 % (len(self._stand_bases), len(ids), len(self.found),
                    self.elapsed(t0)))

    def _pbd_layer_ids(self) -> set[int]:
        """解析所有 PBD 圖層清單，回傳 layer_id ∪ group_layer_id 集合（有快取）。"""
        cache = self.work / 'cache' / ('pbd_layers_%s.json' % self.game_id)
        if cache.is_file():
            try:
                obj = json.loads(cache.read_text(encoding='utf-8'))
                if obj.get('ok') and obj.get('ids'):
                    self.log('  [pbd] cache hit: %d ids' % len(obj['ids']))
                    return set(obj['ids'])
            except Exception:
                pass
        entries = [(ce, self.corpus.root / ce.path) for ce in
                   getattr(self.corpus, 'entries', {}).values()
                   if ce.kind == T_PBD and ce.path]
        ids: set[int] = set()
        ok = fail = 0
        pbdmod = None
        try:
            from .formats import pbd as pbdmod  # 純 Python 4s0/ns0 解析器
        except Exception as ex:
            self.log('  [pbd] formats.pbd 不可用（%s），改用 pbd2json' % ex)
        if pbdmod is not None:
            for ce, path in entries:
                try:
                    tree, _info = pbdmod.parse_pbd(path.read_bytes())
                    rec = pbdmod.layer_records(tree)
                    layers = rec.get('layers') if isinstance(rec, dict) else None
                    if not layers:
                        continue
                    ok += 1
                    for layer in layers:
                        for key in ('layer_id', 'group_layer_id'):
                            lid = layer.get(key)
                            if isinstance(lid, int):
                                ids.add(lid)
                except Exception:
                    fail += 1
        else:  # fallback：外部 pbd2json（PowerShell + CLR4）
            ps_script = ROOT / 'tools' / 'pbd2json.ps1'
            pbd_exe = ROOT / 'lib' / 'pbd2json.exe'
            for ce, path in entries:
                arr = _run_pbd2json(path, ps_script, pbd_exe)
                if arr is None:
                    fail += 1
                    continue
                ok += 1
                for layer in arr:
                    if not isinstance(layer, dict):
                        continue
                    for key in ('layer_id', 'group_layer_id'):
                        lid = layer.get(key)
                        if isinstance(lid, int):
                            ids.add(lid)
        self.stats['pbd'] = {'files': len(entries), 'ok': ok, 'failed': fail}
        cache.parent.mkdir(parents=True, exist_ok=True)
        try:
            cache.write_text(json.dumps({'ok': ok, 'failed': fail,
                                         'ids': sorted(ids)}), encoding='utf-8')
        except OSError:
            pass
        return ids

    def derive_voice_families(self) -> None:
        """語音家族補齊（家族補號 / b|c 替代 take / loop 語音交叉）。

        只以「已命中的家族」為界（見 rules.VOICE_FAMILY_EVIDENCE），
        全部候選仍由 hash 表驗證。
        """
        t0 = time.time()
        n = 0
        for name, rule in R.family_candidates(list(self.found.values())):
            self.probe(Candidate(name, rule, 'voice-family'))
            n += 1
        self.flush()
        self.stats['voice_family_candidates'] = n
        self.log('  [voice-family] %d candidates -> %d found (%s)'
                 % (n, len(self.found), self.elapsed(t0)))

    def _found_fingerprint(self) -> str:
        import hashlib
        h = hashlib.md5()
        for key in sorted(self.found):
            h.update(key.encode())
        return h.hexdigest()

    def _family_cache_path(self) -> Path:
        return self.work / 'cache' / ('family_%s_v%d.json'
                                      % (self.game_id, self.CACHE_VERSION))

    def _save_family_cache(self, fp: str, pre: set) -> None:
        path = self._family_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        additions = {h: n for h, n in self.found.items() if h not in pre}
        obj = {'fingerprint': fp, 'code': self._code_stamp(),
               'additions': additions,
               'prov': {h: self.prov.get(h) for h in additions}}
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)

    def _load_family_cache(self, fp: str) -> bool:
        path = self._family_cache_path()
        if not path.is_file():
            return False
        try:
            obj = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return False
        if obj.get('fingerprint') != fp or obj.get('code') != self._code_stamp():
            return False
        added = 0
        for h, name in (obj.get('additions') or {}).items():
            if h in self.found:
                continue
            self.found[h] = name
            prov = (obj.get('prov') or {}).get(h) or {}
            self.prov[h] = prov
            rule = prov.get('rule') or 'family'
            self.rule_hits[rule] = self.rule_hits.get(rule, 0) + 1
            mod = _module_of(prov.get('source') or '')
            self.module_hits[mod] = self.module_hits.get(mod, 0) + 1
            added += 1
        self.stats['family_cache_added'] = added
        return True

    def derive_family_completion(self) -> None:
        """通用機制家族補齊（引擎層 + 資料驅動文法，無遊戲專屬名字）。"""
        t0 = time.time()
        from .grammar import family_candidates
        names_db = list(self.found.values())

        def atlas_names():
            for name in names_db:
                base, ext = os.path.splitext(name)
                if ext.lower() in ('.png', '.tlg'):
                    yield base + '.tjs'

        # KiriKiriZ/KAG/Yuzusoft 引擎框架固定腳本（引擎層 profile）
        try:
            from .engine.krkrz_scripts import KRKRZ_ENGINE_SCRIPTS
        except Exception:
            KRKRZ_ENGINE_SCRIPTS = ()
        if KRKRZ_ENGINE_SCRIPTS:
            probed, added = self.probe_fast(
                iter(KRKRZ_ENGINE_SCRIPTS), 'engine-script-profile', 'engine')
            self.stats['engine_script_candidates'] = probed
            self.log('  [engine-scripts] %d profile names (+%d) -> %d found'
                     % (probed, added, len(self.found)))

        probed, added = self.probe_fast(atlas_names(), 'engine-atlas', 'engine')
        self.stats['atlas_candidates'] = probed
        self.log('  [atlas] %d candidates (+%d) -> %d found (%s)'
                 % (probed, added, len(self.found), self.elapsed(t0)))

        names_db = list(self.found.values())
        probed, added = self.probe_fast(
            family_candidates(names_db), 'grammar-family', 'grammar')
        self.stats['grammar_candidates'] = probed
        self.log('  [grammar] %d candidates (+%d) -> %d found (%s)'
                 % (probed, added, len(self.found), self.elapsed(t0)))

        import collections
        ext_count = collections.Counter()
        for name in self.found.values():
            _b, ext = os.path.splitext(name)
            if ext:
                ext_count[ext.lower()] += 1
        exts = [e for e, c in ext_count.most_common() if c >= 2]
        stems = sorted(self._stem_pool, key=lambda x: (len(x), x))

        def stem_ext_names():
            for stem in stems:
                for ext in exts:
                    yield stem + ext
                low = stem.lower()
                if low != stem:
                    for ext in exts:
                        yield low + ext

        probed, added = self.probe_fast(stem_ext_names(), 'stem-ext-fill',
                                        'stem')
        self.stats['stem_pool'] = len(stems)
        self.stats['stem_ext_candidates'] = probed
        self.log('  [stem-ext] %d stems x %d exts, %d candidates (+%d) '
                 '-> %d found (%s)'
                 % (len(stems), len(exts), probed, added, len(self.found),
                    self.elapsed(t0)))

        def stem_locale_names():
            n = 0
            for stem in stems[:30000]:
                if len(stem) > 24 or any(c.isdigit() for c in stem):
                    continue
                for loc in ('_jp', '_cn', '_en', '_tw'):
                    for ext in exts:
                        yield stem + loc + ext
                        n += 1
                        if n > 2_000_000:
                            return

        probed, added = self.probe_fast(stem_locale_names(),
                                        'stem-locale', 'stem')
        self.stats['stem_locale_candidates'] = probed
        self.log('  [stem-locale] %d candidates (+%d) -> %d found (%s)'
                 % (probed, added, len(self.found), self.elapsed(t0)))

        # SE 家族：★殴る６ -> 尾數全形/半形變體
        se_exts = [e for e in exts if e in ('.ogg', '.wav', '.opus', '.sli')]

        def se_digit_names():
            n = 0
            for stem in stems:
                if not re.search(r'[0-9０-９]$', stem):
                    continue
                base = stem[:-1]
                for d in '0123456789０１２３４５６７８９':
                    for ext in se_exts:
                        yield base + d + ext
                        n += 1
                for ext in se_exts:
                    yield base + ext
                    n += 1
                if n > 800000:
                    break

        if se_exts:
            probed, added = self.probe_fast(se_digit_names(),
                                            'se-digit-variant', 'stem')
            self.stats['se_digit_candidates'] = probed
            self.log('  [se-digit] %d candidates (+%d) -> %d found (%s)'
                     % (probed, added, len(self.found), self.elapsed(t0)))

        # 引擎同族 wrapper 前綴互換（thum_/savethum_/bgthum_/chthum_ 等，
        # 前綴集合由已命中名字自己歸納，不寫死）
        prefixes = collections.Counter()
        for name in self.found.values():
            stem, _ext = os.path.splitext(name)
            if '_' in stem:
                head = stem.split('_', 1)[0]
                if 2 <= len(head) <= 12 and head.isalpha() and head.islower():
                    prefixes[head] += 1
        prefix_list = [p for p, c in prefixes.items() if c >= 3][:40]

        def wrapper_swap_names():
            for name in list(self.found.values()):
                stem, ext = os.path.splitext(name)
                if '_' not in stem:
                    continue
                head, rest = stem.split('_', 1)
                if head not in prefix_list or not rest:
                    continue
                for p in prefix_list:
                    if p != head:
                        yield p + '_' + rest + ext

        if prefix_list:
            probed, added = self.probe_fast(wrapper_swap_names(),
                                            'wrapper-swap', 'engine')
            self.stats['wrapper_swap_candidates'] = probed
            self.log('  [wrapper-swap] %d prefixes, %d candidates (+%d) '
                     '-> %d found (%s)'
                     % (len(prefix_list), probed, added, len(self.found),
                        self.elapsed(t0)))

        # 3-letter code 家族（如 chaticon_yuk）：prefix + 3 letters 窮舉
        import collections as _c2
        letter_fams: dict[tuple[str, str], set[str]] = _c2.defaultdict(set)
        for name in list(self.found.values()):
            stem, ext = os.path.splitext(name)
            m = re.match(r'^(.*?[_-])([a-z]{3})$', stem)
            if m:
                letter_fams[(m.group(1), ext)].add(m.group(2))

        def letter_code_names():
            for (base, ext), codes in letter_fams.items():
                if len(codes) < 3:
                    continue
                for a in letters_all:
                    for b in letters_all:
                        for c in letters_all:
                            yield base + a + b + c + ext

        letters_all = 'abcdefghijklmnopqrstuvwxyz'
        if letter_fams:
            probed, added = self.probe_fast(letter_code_names(),
                                            'letter-code', 'grammar')
            self.stats['letter_code_candidates'] = probed
            self.log('  [letter-code] %d families, %d candidates (+%d) '
                     '-> %d found (%s)'
                     % (len(letter_fams), probed, added, len(self.found),
                        self.elapsed(t0)))

        # 前綴 + token 組合（引擎動態載入的腳本名，如 scnchart_ui_ + sel）
        token_pool = sorted(self._token_pool, key=lambda x: (len(x), x))

        def token_compose_names():
            n = 0
            seen = 0
            for name in list(self.found.values()):
                stem, ext = os.path.splitext(name)
                if not stem or stem[-1] in '_.-':
                    continue
                base = stem + '_'
                for tok in token_pool:
                    yield base + tok + ext
                    n += 1
                    if n > 400000:
                        return

        if token_pool:
            probed, added = self.probe_fast(token_compose_names(),
                                            'token-compose', 'grammar')
            self.stats['token_compose_candidates'] = probed
            self.log('  [token-compose] %d tokens, %d candidates (+%d) '
                     '-> %d found (%s)'
                     % (len(token_pool), probed, added, len(self.found),
                        self.elapsed(t0)))

        # 非 ASCII stem 尾端單字母變體（dsp_電話 -> dsp_電話f 等）
        letters = 'abcdefghijklmnopqrstuvwxyz'

        def tail_letter_names():
            for name in list(self.found.values()):
                stem, ext = os.path.splitext(name)
                if not stem or stem[-1].isascii():
                    continue
                for ch in letters:
                    yield stem + ch + ext

        probed, added = self.probe_fast(tail_letter_names(),
                                        'tail-letter', 'grammar')
        self.stats['tail_letter_candidates'] = probed
        self.log('  [tail-letter] %d candidates (+%d) -> %d found (%s)'
                 % (probed, added, len(self.found), self.elapsed(t0)))

        # 語音 stem 尾字母/號碼變體（tuk_306_0031b -> ...a；PSB voice.storage）
        def voice_variant_names():
            pat = re.compile(r'^(.+?_\d{4}|.+?_\d{3}_\d{4})([a-z])?$')
            groups = {}
            for stem in stems:
                m = pat.match(stem)
                if not m or len(stem) > 60:
                    continue
                base, tail = m.group(1), m.group(2) or ''
                nums = re.findall(r'\d+', stem)
                if nums:
                    groups.setdefault(base, set())
            for base in groups:
                m2 = re.match(r'^(.*?)(\d+)([a-z]?)$', base)
                if not m2:
                    continue
                pref, numstr, tail = (m2.group(1), m2.group(2),
                                      m2.group(3))
                num = int(numstr)
                width = len(numstr)
                for d in range(max(0, num - 3), num + 4):
                    for ch in 'abcde':
                        for ext in ('.ogg', '.wav'):
                            yield '%s%0*d%s%s' % (pref, width, d, ch, ext)

        probed, added = self.probe_fast(voice_variant_names(),
                                        'voice-variant', 'stem')
        self.stats['voice_variant_candidates'] = probed
        self.log('  [voice-variant] %d candidates (+%d) -> %d found (%s)'
                 % (probed, added, len(self.found), self.elapsed(t0)))

        # 圖層 frame 數列：日文基底 + '_' + 號碼 + 圖檔（月望a_534.tlg 等）
        def layer_frame_names():
            n = 0
            for stem in stems:
                if not stem or stem[-1].isdigit():
                    continue
                if not any(ord(c) > 0x7f for c in stem):
                    continue
                if len(stem) > 24 or any(c in stem for c in '()（）[]【】'):
                    continue
                for num in range(0, 2001):
                    for ext in ('.tlg', '.png'):
                        yield stem + '_' + str(num) + ext
                        n += 1
                        if n > 4_000_000:
                            return

        probed, added = self.probe_fast(layer_frame_names(),
                                        'layer-frame', 'stem')
        self.stats['layer_frame_candidates'] = probed
        self.log('  [layer-frame] %d candidates (+%d) -> %d found (%s)'
                 % (probed, added, len(self.found), self.elapsed(t0)))

        # 括號變體家族：`■蝉の声(午前｜屋内)` 的括號內是「場景/地點」槽，
        # 同家族或其它家族出現過的值可以互換（全形/半形也互換）。
        import collections as _col
        fam: dict[str, set[str]] = _col.defaultdict(set)
        for stem in stems:
            m = re.match(r'^(.*)[（(]([^（()）]*)[）)]$', stem)
            if m and len(m.group(2)) <= 24:
                fam[m.group(1)].add(m.group(2))
        inner_pool = set()
        _sep = re.compile(r'[｜|／/・,、]')
        for inners in fam.values():
            for inner in inners:
                inner_pool.add(inner)
                parts = [x for x in _sep.split(inner) if x]
                for x in parts:
                    inner_pool.add(x)
                if 2 <= len(parts) <= 4:
                    for i in range(len(parts)):
                        for j in range(i + 1, len(parts) + 1):
                            inner_pool.add('｜'.join(parts[i:j]))

        def paren_names():
            n = 0
            for base, _inners in fam.items():
                fam_n = 0
                for inner in sorted((x for x in inner_pool if x), key=lambda x: (len(x), x))[:600]:
                    for l, r in (('(', ')'), ('（', '）')):
                        for ext in exts:
                            yield base + l + inner + r + ext
                            n += 1
                            fam_n += 1
                            if fam_n >= 8000:
                                break
                        if fam_n >= 8000:
                            break
                    if fam_n >= 8000:
                        break
                if n > 4_000_000:
                    break

        if fam:
            probed, added = self.probe_fast(paren_names(), 'paren-variant',
                                            'stem')
            self.stats['paren_candidates'] = probed
            self.log('  [paren] %d families, %d candidates (+%d) '
                     '-> %d found (%s)'
                     % (len(fam), probed, added, len(self.found),
                        self.elapsed(t0)))

    def _engine_digest_cache(self) -> dict:
        """讀取校准器產生的 PackinOne digest cache（entry hash -> suffix）。"""
        path = self.work / 'engine' / ('packinone_digest_%s.json' % self.game_id)
        if not path.is_file():
            return {}
        try:
            obj = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return {}
        if isinstance(obj, dict):
            obj.pop('_meta', None)
            return {k.upper(): v for k, v in obj.items() if isinstance(v, str)}
        return {}

    def derive_template_names(self) -> None:
        """用引擎 digest cache + 遊戲自己的格式字串模板推導 runtime 名字。"""
        t0 = time.time()
        cache = self._engine_digest_cache()
        if not cache or not self._templates:
            self.stats['engine_digest_cache'] = len(cache)
            return
        hex_tpls = [t for t in self._templates
                    if ('%08x' in t or '%08X' in t) and '.' in t
                    and ' ' not in t]
        prefix_tpls = []
        for t in self._templates:
            if t.count('%s') != 1 or '%08' in t or '%d' in t or '%x' in t:
                continue
            core = t.replace('%s', '')
            if (2 <= len(core) <= 40 and core.endswith('_')
                    and all(ch.isascii() and (ch.isalnum() or ch == '_')
                            for ch in core)):
                prefix_tpls.append(t)
        chars = [c for c in self._char_pool
                 if 1 <= len(c) <= 16 and not c.isascii()
                 and not any(ch in c for ch in '%/\\.')]
        self.stats['engine_digest_cache'] = len(cache)
        self.stats['engine_digest_templates'] = {
            'hex': len(hex_tpls), 'prefix': len(prefix_tpls),
            'chars': len(chars)}
        self.log('  [engine-digest] cache=%d hex_tpl=%d prefix_tpl=%d chars=%d'
                 % (len(cache), len(hex_tpls), len(prefix_tpls), len(chars)))
        if not (hex_tpls and prefix_tpls and chars):
            return

        def expand(tpl: str, prefix: str, lo: str, hi: str):
            out: list[str] = []
            i = 0
            first_hex = True
            while i < len(tpl):
                if tpl.startswith('%s', i):
                    out.append(prefix)
                    i += 2
                    continue
                if tpl.startswith('%08x', i) or tpl.startswith('%08X', i):
                    out.append(lo if first_hex else hi)
                    first_hex = False
                    i += 4
                    continue
                if tpl.startswith('%%', i):
                    out.append('%')
                    i += 2
                    continue
                out.append(tpl[i])
                i += 1
            res = ''.join(out)
            return None if '%' in res else res
        n = 0
        pending = 0
        for h, suffix in cache.items():
            if h in self.found or len(suffix) < 16:
                continue
            lo, hi = suffix[:8], suffix[8:16]
            for pt in prefix_tpls:
                for char in chars:
                    prefix = expand(pt, char, lo, hi)
                    if not prefix:
                        continue
                    for ht in hex_tpls:
                        cand = expand(ht, prefix, lo, hi)
                        if cand:
                            self.probe(Candidate(cand, 'engine-digest',
                                                 'engine'))
                            n += 1
                            pending += 1
            if pending > 200000:
                self.flush()
                pending = 0
        self.flush()
        self.stats['engine_digest_candidates'] = n
        self.log('  [engine-digest] %d candidates -> %d found (%s)'
                 % (n, len(self.found), self.elapsed(t0)))

    def gather_dirs(self) -> None:
        """還原「明文目錄」：Hxv4 表按目錄分組，domain key 就是目錄 entry 的 hash。

        目錄名稱由 group 內容特徵歸納（見 dirs.py），並用「group 在表中的順序 =
        目錄名 ASCII 排序」交叉驗證。目錄無法用 64-hex hash 驗證，所以單獨列出。
        """
        t0 = time.time()
        try:
            from . import dirs as dirsmod
            name_of = {h: n for h, n in self.found.items()}
            res = dirsmod.derive_dirs(self.corpus, name_of,
                                      engine_dirs=sorted(self._engine_dirs))
        except Exception as ex:
            self.log('  [dirs] 目錄推導失敗（%s: %s），跳過'
                     % (type(ex).__name__, ex))
            return
        self.dir_map = dict(res.get('map') or {})
        self.stats['directories'] = {
            'recovered': len(self.dir_map),
            'unresolved': len(res.get('problems') or []),
        }
        if self.dir_map:
            self.log('  [dirs] %d 個目錄: %s' % (
                len(self.dir_map),
                ', '.join(sorted(self.dir_map.values()))[:200]))
        for p in (res.get('problems') or [])[:6]:
            self.log('  [dirs] 未解 group %s: %s'
                     % (p.get('domain', '?')[:16], p.get('reason', '')))
        self.stats['dir_problems'] = (res.get('problems') or [])[:40]
        self.stats['dir_score'] = res.get('score')
        self.stats['dir_candidates'] = len(res.get('candidates') or [])
        self.stats['dir_seconds'] = round(time.time() - t0, 1)

    # --------------------------------------------------------------- output
    def run(self) -> dict:
        t_total = time.time()
        self.log('Hxv4 檔名推導開始: game=%s workers=%d' %
                 (self.game_id, self.workers))
        self.log('game dir: %s' % self.game_dir)
        if getattr(self.reporter, 'log_file', None):
            self.log('log file: %s' % self.reporter.log_file)
        self._run_stage('載入 corpus / 解密 / hash 表', self.load)
        self._run_stage('parser：解析所有邏輯型 entry', self.gather_and_expand)
        self._run_stage('TJS 邏輯分析', self.gather_logic)
        self._run_stage('外部 / 動態證據', self.load_extra_facts)
        self._run_stage('stage 樣板展開', self._flush_stage_pending)
        self._run_stage('bounded family 規則', self.derive_bounded)
        self._run_stage('資料表推導', self.derive_data_tables)
        self._run_stage('PBD 圖層零件', self.gather_pbd_parts)
        self._run_stage('名稱再衍伸 round 1', self.derive)
        self._run_stage('語音家族補齊', self.derive_voice_families)
        self._run_stage('名稱再衍伸 round 2', self.derive)

        def _family_stage():
            fam_fp = self._found_fingerprint()
            if self._load_family_cache(fam_fp):
                self.log('  family/template cache hit -> %d found'
                         % len(self.found))
                return
            pre = set(self.found)
            self.derive_family_completion()
            self.derive()
            self.derive_template_names()
            self.derive()
            self._save_family_cache(fam_fp, pre)

        self._run_stage('通用文法 + 模板 + 引擎 digest', _family_stage)
        self._run_stage('明文目錄還原', self.gather_dirs)
        summary = self.report()
        out_hits = dict(self.found)
        out_hits.update(getattr(self, 'dir_map', {}) or {})
        n = write_lst(out_hits, self.out)
        summary['lst'] = str(self.out)
        summary['lst_lines'] = n
        self.log('WROTE %s (%d lines)' % (self.out, n))
        self.close()
        summary['seconds'] = round(time.time() - t_total, 1)
        report_path = self.work / 'report' / ('%s.json' % self.game_id)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                               encoding='utf-8')
        md = self.work / 'report' / ('%s.md' % self.game_id)
        md.write_text(self.render_markdown(summary), encoding='utf-8')
        self.log('report -> %s' % report_path)
        return summary

    # --------------------------------------------------------------- report
    def report(self) -> dict:
        total = len(self.table) + len(self.corpus.plain)
        warning = len(self.table.warning)
        missing_hashes = [h for h in self.table.provenance if h not in self.found]
        # 依內容型態統計
        by_kind: dict[str, dict[str, int]] = {}
        entries = getattr(self.corpus, 'entries', {}) or {}
        for h, (arc, idx) in self.table.provenance.items():
            ce = entries.get(h.lower()) or entries.get(h)
            kind = ce.kind if ce is not None else '?'
            slot = by_kind.setdefault(kind, {'total': 0, 'found': 0})
            slot['total'] += 1
            if h in self.found:
                slot['found'] += 1
        missing_entries = []
        for h in missing_hashes:
            arc, idx = self.table.provenance[h]
            ce = entries.get(h.lower()) or entries.get(h)
            missing_entries.append({
                'hash': h, 'archive': arc, 'index': idx,
                'kind': ce.kind if ce is not None else '?',
                'size': ce.size if ce is not None else None,
            })
        missing_entries.sort(key=lambda r: (r['archive'], r['index']))
        summary = {
            'game': self.game_id,
            'game_dir': str(self.game_dir),
            'total_hashes': total,
            'table_hashes': len(self.table),
            'plain_names': len(self.corpus.plain),
            'warning_entries': warning,
            'recovered': len(self.found),
            'missing': len(missing_hashes),
            'coverage_pct': round(100.0 * len(self.found) / max(total, 1), 3),
            'by_kind': dict(sorted(by_kind.items())),
            'by_rule': dict(sorted(self.rule_hits.items(),
                                   key=lambda kv: -kv[1])),
            'by_module': dict(sorted(self.module_hits.items(),
                                     key=lambda kv: -kv[1])),
            'stats': self.stats,
            'rules': [{'id': rid, 'evidence': ev, 'hits': self.rule_hits.get(rid, 0)}
                      for rid, ev in R.RULES],
            'missing_entries': missing_entries,
        }
        summary['directories'] = {
            'count': len(getattr(self, 'dir_map', {}) or {}),
            'entries': dict(sorted((self.dir_map or {}).items(),
                                   key=lambda kv: kv[1])),
            'problems': self.stats.get('dir_problems', []),
        }
        return summary

    def render_markdown(self, s: dict) -> str:
        lines = []
        lines.append('# %s 檔名還原報告\n' % s['game'])
        lines.append('* 候選 hash 總數：**%d**（Hxv4 表 %d + index 明文 %d；warning entry %d）'
                     % (s['total_hashes'], s['table_hashes'], s['plain_names'],
                        s['warning_entries']))
        lines.append('* 命中：**%d**（%.3f%%）' % (s['recovered'], s['coverage_pct']))
        lines.append('* 未命中：**%d**（見 missing_entries）\n' % s['missing'])
        lines.append('## 各內容型態覆蓋率\n')
        lines.append('| kind | 總數 | 命中 | % |')
        lines.append('|---|---|---|---|')
        for kind, v in s['by_kind'].items():
            pct = 100.0 * v['found'] / max(v['total'], 1)
            lines.append('| %s | %d | %d | %.1f%% |' % (kind, v['total'], v['found'], pct))
        lines.append('\n## 規則貢獻\n')
        lines.append('| rule | hits | evidence |')
        lines.append('|---|---|---|')
        for r in s['rules']:
            if r['hits'] or r['id'] in ('direct', 'generic_suffix'):
                ev = (r['evidence'] or '').replace('|', '/')
                lines.append('| %s | %d | %s |' % (r['id'], r['hits'], ev))
        dirs_info = s.get('directories') or {}
        if dirs_info.get('entries'):
            lines.append('\n## 明文目錄（Hxv4 domain group；16-hex key 無法用 64-hex '
                         'hash 驗證，已用內容特徵 + 群組排序交叉驗證）\n')
            lines.append('| dir | domain key |')
            lines.append('|---|---|')
            for key, name in dirs_info['entries'].items():
                lines.append('| %s | %s |' % (name, key))
            if dirs_info.get('problems'):
                lines.append('\n* 未解 group：%d（見 report JSON）'
                             % len(dirs_info['problems']))
        lines.append('\n## 未命中清單（前 200 筆）\n')
        lines.append('| hash | archive | idx | kind | size |')
        lines.append('|---|---|---|---|---|')
        for r in s['missing_entries'][:200]:
            lines.append('| %s | %s | %d | %s | %s |'
                         % (r['hash'][:16], r['archive'], r['index'], r['kind'],
                            r['size']))
        lines.append('')
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# 平行 parser stage（ProcessPoolExecutor + spawn）
# ---------------------------------------------------------------------------
_PARSE_MODS: list | None = None


def _parse_init() -> None:
    global _PARSE_MODS
    from .formats import textfmt
    mods = [textfmt]
    have = set()
    for name in ('psb', 'pbd', 'tjs'):
        try:
            mod = __import__('hxlib.formats.' + name, fromlist=['iter_facts'])
        except Exception:
            continue
        if hasattr(mod, 'iter_facts'):
            mods.append(mod)
            have.add(name)
    try:
        from .formats import psbraw
        mods.append(psbraw)
    except Exception:
        pass
    try:
        from .formats import tjsraw
        mods.append(tjsraw)
    except Exception:
        pass
    _PARSE_MODS = mods


def _parse_items(items: list[dict]) -> list[tuple[str, str, list]]:
    """items: [{'hash','archive','index','kind','path'}]；回傳 (modname,src,facts)。"""
    import types
    mods = _PARSE_MODS or []
    out: list[tuple[str, str, list]] = []
    for item in items:
        try:
            data = Path(item['path']).read_bytes()
        except OSError:
            continue
        entry = types.SimpleNamespace(**item)
        for mod in mods:
            mkinds = getattr(mod, 'KINDS', None)
            if mkinds is not None and item['kind'] not in mkinds:
                continue
            try:
                got = mod.iter_facts(entry, data)
            except Exception:
                continue
            if got:
                facts = list(got)
                modname = getattr(mod, '__name__', '?').rsplit('.', 1)[-1]
                src = '%s:%s#%d' % (modname, item['archive'], item['index'])
                out.append((modname, src, facts))
    return out


def _run_parse_tasks(chunks: list[list[dict]], workers: int,
                     progress_cb=None) -> list[list]:
    """以 ProcessPoolExecutor 平行跑 parser；回傳與 chunks 同序的結果。"""
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor, as_completed
    ctx = mp.get_context('spawn')
    results: list[list] = [[] for _ in chunks]
    total_chunks = len(chunks)
    total_files = sum(len(c) for c in chunks)
    done_chunks = 0
    done_files = 0
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx,
                             initializer=_parse_init) as ex:
        futs = {ex.submit(_parse_items, chunk): i
                for i, chunk in enumerate(chunks)}
        for fut in as_completed(futs):
            idx = futs[fut]
            try:
                results[idx] = fut.result()
            except Exception:
                results[idx] = []
            done_chunks += 1
            done_files += len(chunks[idx])
            if progress_cb is not None:
                try:
                    progress_cb(done_chunks, total_chunks, done_files,
                                total_files)
                except Exception:
                    pass
    return results


def _run_pbd2json(path, ps_script, pbd_exe) -> list | None:
    """用 pbd2json 解析一個 PBD 圖層清單（先直呼；CLR 2.0 不可用時走 PowerShell+CLR4）。"""
    import subprocess
    exe = str(pbd_exe)
    if Path(exe).is_file():
        try:
            out = subprocess.run([exe, str(path)], capture_output=True, timeout=120)
            txt = out.stdout.decode('utf-8', 'replace')
            if txt.strip().startswith('['):
                return json.loads(txt)
        except Exception:
            pass
    if not Path(ps_script).is_file():
        return None
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-File', str(ps_script), '-Exe', exe, '-File', str(path)],
            capture_output=True, timeout=180)
        txt = out.stdout.decode('utf-8', 'replace')
        if not txt.strip().startswith('['):
            return None
        return json.loads(txt)
    except Exception:
        return None


def _module_of(source: str) -> str:
    if not source:
        return '?'
    for prefix in ('textfmt', 'psb', 'pbd', 'tjs', 'logic', 'derived', 'manual'):
        if source.startswith(prefix) or (' ' + prefix) in source:
            return prefix
    return source.split(':', 1)[0]


# ------------------------------------------------------------------- helpers
def _garbro_target_dir(garbro_dir) -> Path:
    """找出 GARbro 的安裝根目錄。

    GARbro 只會用 ``DirectoryCatalog(根目錄, "Arc*.dll")`` 載入外掛，因此
    ``ArcHxv4.dll`` 必須放在 **GARbro.GUI.exe 同一層**；放在 ``ArcHxv4\\``
    子資料夾不會被載入（會退回內建 XP3 reader：顯示假名或無法開啟）。

    使用者可能選到 GARbro 根目錄、或先前誤選的 ``ArcHxv4`` 子資料夾，
    這裡都統一解析到根目錄。
    """
    p = Path(garbro_dir)
    for cand in (p, p.parent):
        if (cand / 'GARbro.GUI.exe').is_file() or (cand / 'GameRes.dll').is_file():
            return cand
    if p.name.lower() == 'archxv4':
        return p.parent
    return p


def install_garbro(garbro_dir, out_lst, drip_path, log=print):
    """把 ArcHxv4.dll 放進 GARbro 根目錄，清單與 drip 放進 GameData\\。

    ``ArcHxv4.dll`` 必須和 ``GARbro.GUI.exe`` 同層（GARbro 只掃描根目錄的
    ``Arc*.dll``），但清單與 drip 依社群慣例放在 ``GameData\\``；外掛會優先
    讀 ``GameData\\HxNames.lst`` 與 ``GameData\\drip_program.json``。
    """
    import shutil
    dst = _garbro_target_dir(garbro_dir)
    dst.mkdir(parents=True, exist_ok=True)
    data = dst / 'GameData'
    data.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    copied = []
    # 1) 清單（GameData，和社群清單同一位置）
    shutil.copy2(out_lst, data / 'HxNames.lst')
    copied.append('GameData\\HxNames.lst')
    # 2) ArcHxv4.dll（repo 已附 out/；缺檔時才用原始碼編譯）
    dll = root / 'out' / 'ArcHxv4.dll'
    if not dll.is_file():
        import subprocess
        build = root / 'plugin' / 'build.ps1'
        if build.is_file():
            log('  [garbro] 找不到 %s，改跑 plugin/build.ps1 ...' % dll)
            rc = subprocess.call([
                'powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', str(build), '-GarbroDir', str(dst),
                '-OutDir', str(root / 'out')])
            if rc != 0:
                log('  [garbro] 外掛編譯失敗（exit %d）' % rc)
    if dll.is_file():
        shutil.copy2(dll, dst / 'ArcHxv4.dll')
        copied.append('ArcHxv4.dll')
    else:
        log('  [garbro] 找不到 %s，且編譯失敗（GARbro 將看不到真檔名）' % dll)
    # 3) drip_program.json
    if drip_path:
        drip = Path(drip_path)
        if drip.is_file():
            shutil.copy2(drip, data / 'drip_program.json')
            copied.append('GameData\\drip_program.json')
        else:
            log('  [garbro] drip 檔不存在：%s' % drip)
    log('  [garbro] 已安裝到 %s：%s' % (dst, ', '.join(copied)))
    return dst


def parse_args(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description='Hxv4 檔名推導主流程')
    ap.add_argument('--game', required=True, help='game id，例如 tenshi / lllj')
    ap.add_argument('--game-dir', required=True)
    ap.add_argument('--drip', required=True)
    ap.add_argument('--work', default=str(ROOT / 'work'))
    ap.add_argument('--corpus-root', default=None)
    ap.add_argument('--out', default=None)
    ap.add_argument('--workers', type=int, default=None)
    ap.add_argument('--no-reuse', action='store_true',
                    help='忽略 corpus 快取，重新解密')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--log-file', default=None,
                    help='把 log 同時寫到這個檔案（預設 work/logs/<game>_<timestamp>.log）')
    ap.add_argument('--garbro-dir', default=None,
                    help='GARbro 安裝根目錄（GARbro.GUI.exe 所在資料夾）；完成後把 ArcHxv4.dll 放根目錄，HxNames.lst 與 drip_program.json 放 GameData\\')
    return ap.parse_args(argv)


def main(argv=None) -> int:
    import time
    args = parse_args(argv)
    log_file = args.log_file
    if log_file is None and not args.quiet:
        stamp = time.strftime('%Y%m%d-%H%M%S')
        log_file = str(Path(args.work) / 'logs' /
                       ('%s_%s.log' % (args.game, stamp)))
    p = Pipeline(
        args.game, args.game_dir, args.drip,
        work=args.work,
        corpus_root=args.corpus_root,
        reuse_corpus=not args.no_reuse,
        out=args.out,
        verbose=not args.quiet,
        workers=args.workers,
        log_file=log_file,
    )
    t0 = time.time()
    summary = p.run()
    print('\nRESULT: %d / %d = %.2f%%'
          % (summary['recovered'], summary['total_hashes'],
             summary['coverage_pct']))
    print('time: %.1fs' % (time.time() - t0))
    if log_file:
        print('log : %s' % log_file)
    if args.garbro_dir:
        print('garbro install ...')
        install_garbro(args.garbro_dir, args.out or
                       (Path(args.work) / ('HxNames.%s.lst' % args.game)),
                       args.drip, log=print)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
