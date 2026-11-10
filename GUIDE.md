# 使用指南

這份文件說明如何用本專案對**一款新的 Hxv4 / Cxdec 遊戲**產生完整檔名清單，
以及哪些檔案是每款遊戲必須自己產生的。很多步驟已由工具自動化，可依照
下面的命令順序直接執行。

\---

## 0. 第一次執行（自動準備）

```powershell
pip install -r requirements.txt
```

工具啟動時會自動補充第三方元件（`lib/vendor.py` 的 `bootstrap()`）：

|缺少元件|動作|來源|
|-|-|-|
|PyCryptodome|`pip install -r requirements.txt`|PyPI|
|`vendor/cxdec-hxv4-static-analysis`|下載到 `vendor/`|原始上游 `hktkqj/cxdec-hxv4-static-analysis`|
|KrkrDump（只有動態 dump 需要）|下載 `v1.5.zip` 解壓到 `tools/krkr/`|原始上游 `crskycode/KrkrDump`（GPL-3.0）|
|`tools/krkr/inject_launch.exe`|用本機 MSVC 自動編譯|本專案 `inject_launch.c`|

要完全手動控制時可設 `HXV4_SKIP_AUTO_INSTALL=1`。

\---

## 1. 產生該遊戲的 `drip_program.json`（通常自動）

**這是每款遊戲不同的檔案，本 repo 不提供。** 正常直接跑第 2 步即可：
GUI 或 `run_pipeline.py` 只要給遊戲執行檔，就會在推導前自動呼叫這一步。

要單獨產生（或想先檢查金鑰）時：

```powershell
python run.py key --exe "C:\Game\game.exe" --game "C:\Game" --out work\drip_program.json
```

* `--exe`：遊戲執行檔（較大的那個 `.exe`）。
* `--game`：遊戲資料夾，用來驗證金鑰。
* 成功後可檢查 `work/drip_program.json` 是否存在。

> 若金鑰已在別處取得，也可以直接把 `drip_program.json` 放到 `userdata/` 或
> 用 `--drip` 指定，不一定要自己算。

\---

## 2. 執行主推導

### 圖形介面（推薦，會顯示即時 log）

```powershell
python tools/run_gui.py
```

填入遊戲 ID、遊戲目錄、遊戲執行檔 (.exe)、工作目錄、輸出清單與 GARbro 安裝目錄
（`GARbro.GUI.exe` 所在資料夾），
按「開始推導」。程式會先自動產生 `drip_program.json`，完成後自動安裝 GARbro。

### 指令列

```powershell
python tools/run_pipeline.py --game <game_id> ^
  --game-dir "C:\Game" ^
  --exe "C:\Game\game.exe" ^
  --out "work\HxNames.<game_id>.lst" ^
  --garbro-dir "C:\GARbro"
```

* `--exe`：遊戲執行檔；沒有 `--drip` 時會先自動產生 `drip_program.json`。
* `--drip`：已經有金鑰檔時可直接指定，會跳過自動產生。
* `<game_id>` 自己取一個短代號（例如 `lllj`、`tenshi`、`sanoba`），會用在快取與輸出。
* log 預設寫到 `work/logs/<game_id>_<timestamp>.log`；`--log-file` 可指定，
`--quiet` 關閉 console 輸出。
* 第一次會解密全部 XP3（視大小幾分鐘），之後重跑用 corpus/facts/found 快取
（約 30 秒）。
* `--garbro-dir` 會在完成後把 `ArcHxv4.dll` 放進 GARbro 根目錄，
  `HxNames.lst`、`drip_program.json` 放進 `GameData\`；請指定 GARbro 根目錄
  （`GARbro.GUI.exe` 所在資料夾），GARbro 只會載入根目錄的 `Arc*.dll`。

\---

## 3. 可選：產生 hash 對照表（合併清單時會用到）

```powershell
python tools/build_corpus.py --game <game_id> --game-dir "C:\Game" ^
  --drip "work\drip_program.json" --table-out "work\table_<game_id>.json"
```

\---

## 4. 可選：PackinOne digest 校準（`chthum_*` runtime 縮圖名）

`chthum_<角色>_<digest>.png` 這種名字的 digest 由遊戲引擎的 PackinOne 產生，
純 Python 無法重現；工具用一次性的「引擎校準」取得：

```powershell
python tools/engine_digest_cache.py --game <game_id> ^
  --game-dir "C:\Game" --drip "work\drip_program.json" --work work
```

* 需要遊戲能正常啟動；會自動開關遊戲多次（每批 150 張 PNG）。
* 結果存到 `work/engine/packinone_digest_<game_id>.json`，之後 pipeline 會自動
使用（沒有這個檔也能跑，只是少了這類 runtime 縮圖名）。

\---

## 5. 可選：開遊戲到主頁，動態 dump 檔名

```powershell
python tools/dynamic_dump.py --game <game_id> --game-dir "C:\Game"
```

* 工具會啟動遊戲、自動點過 ATTENTION／logo 到標題畫面，用 KrkrDump 收集
引擎實際打開的檔名，輸出 `work/HxNames.dynamic.<game_id>.lst`。
* 需要 `tools/krkr/KrkrDump.dll` 等檔案；bootstrap 會從 KrkrDump 的**原始
release** 自動下載（GPL-3.0），也可自行放入。
* 若遊戲需要管理員權限或防毒排除（通常需要），請先設定好；遊戲單一實例
鎖住時工具會自動 taskkill 舊的執行個體。

\---

### 動態 dump 的兩種模式

```powershell
# 模式 A：自動進標題主頁後 dump，完成自動關閉遊戲
python tools/dynamic_dump.py --game <game_id> --game-dir "C:\Game" --auto

# 模式 B：只開啟遊戲，讓使用者自己操作；關閉遊戲後收集退出前的所有檔名
python tools/dynamic_dump.py --game <game_id> --game-dir "C:\Game" --manual
```

* `--auto`（預設）：自動點過 ATTENTION／logo 到標題主頁，dump 完自動關閉遊戲。
* `--manual`：工具只啟動遊戲、不點擊也不關閉；使用者操作到主頁／選單／遊戲內，
  關閉遊戲後工具會收集整個過程打開過的所有檔名。
* 兩者都需要 `tools/krkr` 的 KrkrDump；bootstrap 會從原始 release 自動下載。

\---

## 6. 可選：合併靜態 + 動態清單

```powershell
python tools/merge_lists.py --game <game_id> ^
  --base "work\HxNames.<game_id>.lst" ^
  --dynamic "work\HxNames.dynamic.<game_id>.lst" ^
  --table "work\table_<game_id>.json" ^
  --out "work\HxNames.<game_id>.final.lst"
```

每個名字都會用 BLAKE2s 重新驗證，只保留遊戲 table 真的存在的 entry。

\---

## 7. 安裝到 GARbro

主程式（或 GUI）指定 GARbro 根目錄（`GARbro.GUI.exe` 所在資料夾）即可；`ArcHxv4.dll` 必須和 `GARbro.GUI.exe` 同層，
清單與 drip 依社群慣例放在 `GameData\`，完成後會是：

```
ArcHxv4.dll                 ← 安裝時由 plugin/ 編譯並放到根目錄
GameData\HxNames.lst        ← 產生的清單
GameData\drip_program.json  ← 該遊戲的 drip
```

外掛會優先在 `GameData\` 找清單與 drip，找不到才退回 DLL 同層與 `hxv4\`。舊版安裝在根目錄的檔案不影響，
可以自行刪掉。`GameData\HxNames.lst` 只是檔名對照表，不能取代 `ArcHxv4.dll`：tenshi、Sanoba Witch
在沒有外掛時 GARbro-Mod 會顯示 `unknown format`。

也可以用舊版指令：

```powershell
python run.py install --garbro "C:\GARbro" ^
  --drip work\drip_program.json --names work\HxNames.<game_id>.lst
```

之後用 GARbro 開 XP3 就會顯示真檔名。

\---

## 8. 每款遊戲需要自己產生的檔案（不隨 repo 附）

|檔案|產生方式|
|-|-|
|`drip_program.json`|跑第 2 步時自動產生（需要時也可用 `python run.py key --exe ...`）|
|`work/corpus/<game>/`、`work/table_<game>.json`|`run_pipeline.py` / `build_corpus.py` 自動產生|
|`work/engine/packinone_digest_<game>.json`|`engine_digest_cache.py`（可選，步驟 4）|
|`work/HxNames.dynamic.<game>.lst`|`dynamic_dump.py`（可選，步驟 5）|
|`work/HxNames.<game>.lst`|`run_pipeline.py` 產生的最終清單|

本 repo 的 `data/` 有三款已驗證遊戲的完整聯集清單（lllj、tenshi、sanoba）；
其他遊戲請依上面流程自行產生。

\---

## 9. 疑難排解

* **第一次執行很久 / 卡在解密**：正常，XP3 很大時需要幾分鐘；看 console 或
log 的 `解密 n/total (x%)`。
* **log 在哪**：`work/logs/<game_id>_<timestamp>.log`，內容與 console 相同。
* **PyCryptodome 安裝失敗**：手動 `pip install -r requirements.txt`，或設
`HXV4_SKIP_AUTO_INSTALL=1` 後自行處理。
* **遊戲起不來（動態 dump / digest 校準）**：先確認遊戲可直接啟動，必要時用
管理員權限、加防毒排除、清掉殘留的遊戲程序（工具會嘗試 taskkill）。
* **清單裡有某個假名沒被還原**：通常是該 entry 沒有被任何遊戲資料引用；可跑動態 dump 或 digest 校準補強。

