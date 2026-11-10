# 檔名清單（Release data）

本目錄是各遊戲的**最終聯集清單**：本專案推導結果 + 社群維護清單。
格式：`<64-hex BLAKE2s(name)>:<filename>` 與 `<16-hex>:<directory>/`。
所有 64-hex 檔名都用 `BLAKE2s(name + UTF-16LE("xp3hnp"))` 重新驗證過。

|檔案|遊戲|來源|檔名數|目錄數|對應 Hxv4 table|表覆蓋率|
|-|-|-|-|-|-|-|
|`HxNames.lllj.lst`|ライムライト・レモネードジャム|本專案 + [MLChinoo/lllj_hxnames](https://github.com/MLChinoo/lllj_hxnames)|61,311|24|61,310|99.97%（61,310 / 61,327）|
|`HxNames.tenshi.lst`|天使☆騒々 RE-BOOT!|本專案 + [MLChinoo/ten_sz_hxnames](https://github.com/MLChinoo/ten_sz_hxnames)|58,137|25|58,136|99.95%（58,136 / 58,166）|
|`HxNames.sanoba.lst`|Sanoba Witch / Sabbat of the Witch|本專案 + [MLChinoo/sanoba_hxnames](https://github.com/MLChinoo/sanoba_hxnames)|48,000|39|48,000|99.99%（48,000 / 48,006）|

## 來源與合併方式

* **本專案**：`tools/run_pipeline.py` 從遊戲資料與腳本邏輯推導，再用 BLAKE2s 對
  Hxv4 table 驗證；詳細流程見 `GUIDE.md`。
* **社群清單**：來自 [MLChinoo](https://github.com/MLChinoo) 的
  `lllj_hxnames`、`ten_sz_hxnames`、`sanoba_hxnames`（產生工具：
  [MLChinoo/hxv4_unhash_tools](https://github.com/MLChinoo/hxv4_unhash_tools)）。
* **聯集**：以 64-hex hash 去重，同 hash 以本專案的名字優先；16-hex 目錄行
  一併聯集。社群補到本專案沒有的名字，例如 `startup_1.tjs`（lllj / tenshi）
  與 Sanoba 的 `nehaha117_001x.opus`、`soudan1104_035.opus.sli`、`紙／丸める.ogg`。
* 社群清單原本的比對程式已移除；來源與聯集結果直接保留在本目錄。

## 備註

* lllj / tenshi 的 `startup_1.tjs` 是 index 明文／別名，Hxv4 table 沒有這個
  hash，所以「檔名數」比「對應 table」多 1。
* Sanoba 的 48,006 包含 1 筆 index 明文；table 的 48,005 個 hash 中還有 5 個
  沒有檔名，加上該筆明文共 6 筆未解出（執行期動態載入或無靜態線索）。
* 其餘未解出的 hash 都是遊戲執行期才動態載入、或任何資料裡都沒留下字串的檔案；
  清單只收錄能通過 hash 驗證或由社群確認的名字。

## 怎麼直接給 GARbro 用

1. 選擇遊戲清單，複製並改名為 `HxNames.lst`。
2. 放到 **GARbro 的 `GameData\`**（和社群 Hxv4 清單同一位置）。
3. 同一個 `GameData\` 資料夾放該遊戲的 `drip_program.json`；換遊戲時兩者一起換。
4. `ArcHxv4.dll` 仍需放在 GARbro 根目錄（和 `GARbro.GUI.exe` 同層）；
   只有 `GameData\` 清單無法開啟 tenshi、Sanoba Witch 這類 archive。
5. 重開 GARbro 即可。

## 重新產生清單

```powershell
python tools/run_pipeline.py --game lllj --game-dir "C:\path\to\lllj" ^
  --exe "C:\path\to\lllj\limelight_lj.exe" --out work/HxNames.lllj.lst
python tools/merge_lists.py --game lllj --base work/HxNames.lllj.lst ^
  --dynamic work/HxNames.dynamic.lllj.lst --table work/table_lllj.json ^
  --out work/HxNames.lllj.lst
```

要重做「本專案 + 社群」的聯集時，把上一步的結果和社群 `.lst` 依 64-hex
hash 去重（同 hash 保留本專案名字）即可。
