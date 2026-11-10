# 檔名清單（Release data）

本目錄是各遊戲的最終清單：本專案推導結果，有社群清單的遊戲再取聯集。
格式：`<64-hex BLAKE2s(name)>:<filename>` 與 `<16-hex>:<directory>/`。
所有 64-hex 檔名都用 `BLAKE2s(name + UTF-16LE("xp3hnp"))` 重新驗證過。

|檔案|遊戲|來源|檔名數|目錄數|對應 Hxv4 table|表覆蓋率|
|-|-|-|-|-|-|-|
|`HxNames.lllj.lst`|ライムライト・レモネードジャム|本專案 + [MLChinoo/lllj\_hxnames](https://github.com/MLChinoo/lllj_hxnames)|61,311|24|61,310|99.97%（61,310 / 61,327）|
|`HxNames.tenshi.lst`|天使☆騒々 RE-BOOT!|本專案 + [MLChinoo/ten\_sz\_hxnames](https://github.com/MLChinoo/ten_sz_hxnames)|58,137|25|58,136|99.95%（58,136 / 58,166）|
|`HxNames.sanoba.lst`|Sanoba Witch / Sabbat of the Witch|本專案 + [MLChinoo/sanoba\_hxnames](https://github.com/MLChinoo/sanoba_hxnames)|48,000|39|48,000|99.99%（48,000 / 48,006）|
|`HxNames.cafestella.lst`|喫茶ステラと死神の蝶 (CafeStella)|本專案（無社群清單）|48,155|17|48,155|99.76%（48,155 / 48,271）|

## 來源與合併方式

* **本專案**：`tools/run\_pipeline.py` 從遊戲資料與腳本邏輯推導，再用 BLAKE2s 對
Hxv4 table 驗證；詳細流程見 `GUIDE.md`。
* **社群清單**：來自 [MLChinoo](https://github.com/MLChinoo) 的
`lllj\_hxnames`、`ten\_sz\_hxnames`、`sanoba\_hxnames`（產生工具：
[MLChinoo/hxv4\_unhash\_tools](https://github.com/MLChinoo/hxv4_unhash_tools)）。
* **CafeStella**：本專案推導，目前沒有找到對應的社群清單。
* **聯集**：以 64-hex hash 去重，16-hex 目錄行一併聯集。

## 備註

* lllj / tenshi 的 `startup\_1.tjs` 是 index 明文／別名，Hxv4 table 沒有這個
hash，所以「檔名數」比「對應 table」多 1。
* Sanoba 的 48,006 包含 1 筆 index 明文；table 的 48,005 個 hash 中還有 5 個
沒有檔名，加上該筆明文共 6 筆未解出（執行期動態載入或無靜態線索）。
* CafeStella 還有 115 筆未解出（UI 變體、adult 縮圖、影片與少數未引用檔），
沒有可用的靜態字串線索。
* 其餘未解出的 hash 都是遊戲執行期才動態載入、或任何資料裡都沒留下字串的檔案；
清單只收錄能通過 hash 驗證或由社群確認的名字。

## 怎麼直接給 GARbro 用

1. 選擇遊戲清單，複製並改名為 `HxNames.lst`。
2. 放到 **GARbro 的 `GameData\\`**（和社群 Hxv4 清單同一位置）。
3. 同一個 `GameData\\` 資料夾放該遊戲的 `drip\_program.json`；換遊戲時兩者一起換。
4. `ArcHxv4.dll` 仍需放在 GARbro 根目錄（和 `GARbro.GUI.exe` 同層）
5. 重開 GARbro 即可。

## 重新產生清單

```powershell
python tools/run\_pipeline.py --game lllj --game-dir "C:\\path\\to\\lllj" ^
  --exe "C:\\path\\to\\lllj\\limelight\_lj.exe" --out work/HxNames.lllj.lst
python tools/merge\_lists.py --game lllj --base work/HxNames.lllj.lst ^
  --dynamic work/HxNames.dynamic.lllj.lst --table work/table\_lllj.json ^
  --out work/HxNames.lllj.lst
```

