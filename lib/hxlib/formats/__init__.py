"""formats -- 各內容格式的解析器（只負責「看到什麼」，不做 hash、不猜檔名）。

每個 parser 模組提供：

* ``KINDS``：它能吃的 ``hxlib.corpus`` 型態代號 tuple。
* ``iter_facts(entry, data)``：吃一個 corpus entry 與其解密後 bytes，回傳
  ``hxlib.facts.Fact`` 串流。source 可回溯到 ``<archive>#<entry_idx>:<json path>``。

模組不自行 hash 驗證（驗證集中在 verify.py），也不做跨檔組合（rules.py）。
"""
