# tools/krkr

本資料夾**不隨 repo 附第三方二進位檔**（`.gitignore` 已排除 `*.dll` / `*.exe`）。
要做「開遊戲到主頁 dump 檔名」時，請自行取得或建置：

1. **KrkrDump**（第三方程式，GPL-3.0）：
   到 <https://github.com/crskycode/KrkrDump> 取得或自行建置
   `KrkrDump.dll`、`KrkrDumpLoader.exe`，放在這個資料夾。
2. **inject_launch.exe**（本專案 MIT，選用但建議）：
   `inject_launch.c` 是本專案寫的 32-bit 啟動器，用「CreateProcess suspended +
   遠端 LoadLibrary」把 `KrkrDump.dll` 注入遊戲，避開 Detours loader 對部分遊戲
   DRM 的干擾。用 MSVC x86 建置：

   ```
   cl /nologo /MT /O2 inject_launch.c /Fe:inject_launch.exe
   ```

沒有 `inject_launch.exe` 時，`tools/dynamic_dump.py` 會退回使用
`KrkrDumpLoader.exe`。授權說明見 repo 根目錄 `THIRD-PARTY.md`。
