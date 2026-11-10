"""engine/krkrz_scripts.py -- KiriKiriZ / KAG / Yuzusoft 引擎框架固定腳本清單。

這是「引擎層」常數，不是某一款遊戲的專屬規則：KiriKiriZ 發行版與 KAG 框架
會固定載入這些腳本（checkboxlayer、diffimage、keybinder...），Yuzusoft 的
框架另外加上 affinesourcebishamon、scnchart_ui_* 等模組。這些名字通常不會
出現在遊戲自己的資料檔（只存在 XP3 table），因此以引擎 profile 直接探測。
"""

KRKRZ_ENGINE_SCRIPTS = (
    # KAG / KiriKiriZ core scripts
    "checkboxlayer.tjs",
    "diffimage.tjs",
    "diffimage2.tjs",
    "keybinder.tjs",
    "snapcapture.tjs",
    "textspeedex.tjs",
    "vcutfade.tjs",
    "voicematch.tjs",
    "nameframe.tjs",
    "fonteffect.tjs",
    "exgamepad.tjs",
    "standlevel.tjs",
    "nocfchkdialog.tjs",
    "optrevundo.tjs",
    "designmscur.tjs",
    # Yuzusoft framework scripts
    "append_setup_1.tjs",
    "affinesourcebishamon.tjs",
    "scnchart_ui_sel.tjs",
    "scnchart_ui_smp.tjs",
    "scnchart_ui_opt.tjs",
)
