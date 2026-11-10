#!/usr/bin/env python3
"""核心模組的單元測試（不需要遊戲資料，秒級完成）。

    python -m pytest tests/ -q          （有 pytest 時）
    python tests/test_core.py           （沒有 pytest 也能跑：簡單 assert 版）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'lib'))
sys.path.insert(0, str(ROOT / 'lib' / 'hxlib'))

from hxlib import facts, rules              # noqa: E402
from hxlib.formats import textfmt           # noqa: E402
from hxlib.verify import hash_digest, hash_hex  # noqa: E402


# ------------------------------------------------------------------ verify
def test_hash_vector():
    # 遊戲用的 hash = BLAKE2s(utf16le(name) + utf16le("xp3hnp"))
    assert hash_hex('!scnlist.txt').upper() == (
        'C1F625E3A4BB508E082A52A8B032F4B3D2F34FF7FB3A30502574717DE6579126')
    # 同一個名字的 digest 必須是 32 bytes
    assert len(hash_digest('a')) == 32


# ------------------------------------------------------------------- facts
def test_fact_validation():
    assert facts.is_plausible_value('ev101aa.tlg')
    assert facts.is_plausible_value('system/', kind=facts.KIND_DIR)
    assert not facts.is_plausible_value('system', kind=facts.KIND_DIR)
    assert not facts.is_plausible_value('')
    assert not facts.is_plausible_value('a\nb')
    fact = facts.Fact('x.tlg', facts.KIND_NAME, 'test', '', 'kag:image.storage')
    assert fact.key() == (facts.KIND_NAME, 'x.tlg')


def test_fact_writer_roundtrip(tmp_path=None):
    import tempfile
    d = Path(tmp_path or tempfile.mkdtemp())
    p = d / 'f.jsonl'
    w = facts.FactWriter(p)
    w.add(facts.Fact('a.tlg', facts.KIND_NAME, 's1'))
    w.add(facts.Fact('a.tlg', facts.KIND_NAME, 's2'))   # 重複 (kind,value) 只留第一筆
    w.add(facts.Fact('b/', facts.KIND_DIR, 's3'))
    w.close()
    got = list(facts.iter_jsonl(p))
    assert [(f.value, f.kind) for f in got] == [('a.tlg', facts.KIND_NAME),
                                                ('b/', facts.KIND_DIR)]


# ------------------------------------------------------------------- rules
def _expand(value, kind, context):
    return {name for name, _rule in rules.expand_fragment(
        facts.Fact(value, kind, '', '', context))}


def test_chart_rule():
    got = _expand('e01_4', facts.KIND_FRAGMENT, 'chart:quote')
    assert 'e01_4.ks.scn' in got


def test_scnlist_rule():
    got = _expand('【共通】01.ks', facts.KIND_FRAGMENT, 'scnlist:line')
    assert '【共通】01.ks.scn' in got
    assert 'x_【共通】01.ks.scn' in got


def test_soundlist_lower_rule():
    got = _expand('BGM01', facts.KIND_FRAGMENT, 'soundlist:csv:col0')
    assert 'bgm01.opus' in got


def test_cglist_rule():
    got = _expand('thum_ev101', facts.KIND_FRAGMENT, 'cglist:csv:col0')
    assert 'thum_ev101.png' in got
    assert 'thum_ev101.psb' in got


def test_derived_rules():
    got = {n for n, _ in rules.derive_from_name('bgm91.opus')}
    assert 'bgm91.opus.sli' in got
    got = {n for n, _ in rules.derive_from_name('backlog.pbd')}
    assert 'backlog__pack.tlg' in got
    assert 'backlog__bg0.tlg' in got
    got = {n for n, _ in rules.derive_from_name('syslangtext_jp.ini')}
    assert 'syslangtext_cn.ini' in got
    assert 'syslangtext_en.ini' in got


def test_every_rule_has_evidence():
    for rid, evidence in rules.RULES:
        assert evidence and len(evidence) >= 10, rid


# --------------------------------------------------------------- pipe split
def test_pipe_split_fragment():
    # KAG storage 的 `檔案|標籤` 語法：noa_115_0094|RV_風呂
    got = _expand('noa_115_0094|RV_風呂', facts.KIND_FRAGMENT, 'psb:voice.storage')
    assert 'noa_115_0094.ogg' in got
    assert 'noa_115_0094.ogg.sli' in got


# ------------------------------------------------------------------- dirs
def test_dirs_classification():
    from hxlib import dirs
    assert dirs.classify_group_all(['k2compat.tjs', 'k2compat_pad.tjs'])[0][0] == 'k2compat/'
    assert dirs.classify_group_all(['bgm01.opus', 'bgm02.opus'])[0][0] == 'bgm/'
    assert dirs.classify_group_all(['bgthum_a.jpg', 'bgthum_b.jpg'])[0][0] == 'thum/bgthum/'
    assert dirs.classify_group_all(['chthum_x_00.png', 'chthum_y_11.png'])[0][0] == 'thum/chthum/'
    assert dirs.classify_group_all(['about.ks', 'advancedmenu.tjs'])[0][0] == 'sysscn/'
    assert dirs._dir_validation_score('sound/', ['a.ogg', 'b.ogg']) == 1.0
    assert dirs._dir_validation_score('locale/cn/', ['a_cn.ini', 'b_cn.ini']) == 1.0
    assert dirs._dir_validation_score('locale/jp/', ['a_cn.ini', 'b_cn.ini']) < 1.0


# ------------------------------------------------------------------ textfmt
def test_descramble_and_decode():
    # 0xFE 0xFE 標頭 + mode 0 的 scramble（namemine 版本）→ 原文
    body = 'abc,テスト'.encode('utf-16le')
    scrambled = bytearray(body)
    for i in range(0, len(scrambled) - 1, 2):
        hi, lo = scrambled[i], scrambled[i + 1]
        # 反向操作：原始 -> 混淆（mode 0: hi^=1, lo^=(hi&0xfe)）
        o1 = hi ^ 1
        o2 = lo ^ (o1 & 0xFE)
        scrambled[i], scrambled[i + 1] = o1, o2
    data = b'\xfe\xfe\x00\x00\x00' + bytes(scrambled)
    decoded = textfmt.decode_text(data)
    assert decoded is not None
    assert 'abc' in decoded[0]


def test_looks_like_name_list():
    assert textfmt.looks_like_name_list('a.ks.scn\nb.ks.scn\nc.ks.scn\n')
    assert not textfmt.looks_like_name_list('這是一句台詞\n另一句台詞\n')


def test_probe_doc_kind():
    assert textfmt.probe_doc_kind('#CGモード一覧\r\n#サムネール,CG名称,\r\n') == 'cglist'
    assert textfmt.probe_doc_kind(
        '# 開始するシナリオを列挙（フロー確認グラフ生成に利用）\r\nx.ks\r\n') == 'scnheads'
    assert textfmt.probe_doc_kind('[texts]\r\ncommon_on = "ON"\r\n') == 'uitexts'


# --------------------------------------------------------------------- main
def _run_all() -> int:
    fns = [(name, obj) for name, obj in sorted(globals().items())
           if name.startswith('test_') and callable(obj)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print('ok   %s' % name)
        except Exception as ex:  # noqa: BLE001
            failed += 1
            print('FAIL %s: %s: %s' % (name, type(ex).__name__, ex))
    print('%d/%d passed' % (len(fns) - failed, len(fns)))
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    raise SystemExit(_run_all())
