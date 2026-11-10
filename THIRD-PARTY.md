# Third-party components

本專案自行撰寫的部分以 **MIT** 授權釋出（見 `LICENSE`）。下列元件來自其他
專案，請遵循其各自授權。

|路徑 / 套件|來源|授權|
|-|-|-|
|`requirements.txt`：`pycryptodome`|PyCryptodome|BSD-2-Clause / Public Domain|
|`vendor/cxdec-hxv4-static-analysis/`（執行期下載，**不隨附**）|[hktkqj/cxdec-hxv4-static-analysis](https://github.com/hktkqj/cxdec-hxv4-static-analysis)|**無授權條款**（預設保留所有權利）|
|`lib/pbd2json.exe`|[MLChinoo/lllj-composite](https://github.com/MLChinoo/lllj-composite)|MIT（`lib/LICENSE-pbd2json.txt`）|
|`data/HxNames.\*.lst`|本專案推導 + MLChinoo 社群清單（lllj\_hxnames、ten\_sz\_hxnames、sanoba\_hxnames）|本專案推導部分 MIT；社群清單資料依原社群|
|`tools/krkr/KrkrDump.dll`、`KrkrDumpLoader.exe`（**不隨附**，bootstrap 自動下載）|[crskycode/KrkrDump](https://github.com/crskycode/KrkrDump)（原始上游，內含 Detours）|**GPL-3.0**|
|`tools/krkr/inject\_launch.c`（本專案附原始碼）|本專案|MIT|
|`plugin/ArcHxv4.cs`、`lib/hxlib/\\\*`、`run.py`、`lib/vendor.py`|本專案|MIT（`LICENSE`）|

## Python 套件

執行時需要 **PyCryptodome**（Hxv4 table 的 XChaCha20-Poly1305 解密）：

```
pip install -r requirements.txt
```

## cxdec-hxv4-static-analysis：不隨附，執行期下載

金鑰還原與 XP3 容器解析用到的程式來自
[hktkqj/cxdec-hxv4-static-analysis](https://github.com/hktkqj/cxdec-hxv4-static-analysis)。

**該專案沒有附授權條款**（GitHub API 的 `license` 欄位為空），依著作權預設為
「保留所有權利」，因此本專案**不把它的程式碼放進 repo**。第一次執行時自動下載到
`vendor\\\\`（`lib/vendor.py`：有 git 用 git clone，否則抓 main 分支 zip），
之後重複使用。使用本專案即代表你自行向上游取得這些檔案；若上游日後加上授權
條款，請以其為準。

## KrkrDump

動態 dump 用的注入器是第三方專案，**本 repo 不含它的二進位**（`.gitignore` 已排除
`tools/krkr/\\\*.dll`、`\\\*.exe`），由使用者自行取得或編譯放入 `tools/krkr/`。
它的授權是 **GPL-3.0**：如果你要散布「本專案 + KrkrDump」的組合包，整體就必須
符合 GPL-3.0。純粹自己使用則不受影響。

`LICENSE` 只涵蓋本專案自己寫的程式碼。

## 執行時自動準備（bootstrap）

`lib/vendor.py` 的 `bootstrap()` 會在工具啟動時檢查並自動補齊：

* **Python 套件**：缺 `Crypto`（PyCryptodome）→ 執行
`python -m pip install -r requirements.txt`（可用 `HXV4\\\_SKIP\\\_AUTO\\\_INSTALL=1` 停用）。
* **cxdec vendor**：缺 `vendor/cxdec-hxv4-static-analysis` → 從**原始上游**
`hktkqj/cxdec-hxv4-static-analysis` 下載；鏡像來源只有使用者設定
`HXV4\\\_VENDOR\\\_MIRROR` 時才會使用。
* **KrkrDump**：缺 `tools/krkr/KrkrDump.dll` → 從**原始來源** `crskycode/KrkrDump`
的 official release `v1.5.zip` 下載並解壓（不是鏡像倉庫）；只有原始來源失敗時
才退回本機離線備份（`HXV4\\\_KRKR\\\_SOURCE`、`../cxdec-vendor/third\\\_party/krkr`…）。
* **inject\_launch.exe**：缺檔且有 MSVC（vcvarsall）→ 自動編譯
`tools/krkr/inject\\\_launch.c`。

