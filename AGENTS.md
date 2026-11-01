# Agent 開發規範

## 入口與責任

- 使用者入口是根目錄 `main.py`；自有模組在 `cbz2kepub/`，KCC 是獨立 submodule。
- 開始任務先讀與修改範圍有關的文件：架構見 `docs/architecture.md`，設定見 `docs/configuration.md`，開發／測試見 `docs/contributing.md`，驗證界線見 `docs/validation.md`，持久決策見 `.agent/decisions.md`。
- `.agent/` 不保證自動載入；需要時明確讀取。沒有特殊局部規則時，不為每個子目錄新增 AGENTS.md。

## 修改與驗證

- 採能完整解決任務的最小修改；先追查呼叫者、import、mock 目標、路徑與測試，不新增未要求的介面或選項。
- 保留來源只讀、archive 邊界驗證、所有權清單、輸出鎖、容量檢查與交易回復；不能按檔名前綴刪成品。
- KCC 修改集中在必要接點；不要為 wrapper 可處理的資料問題改寫原生 parser 或影像流程。修改／更新 KCC 後先驗證並推送，再更新主專案 gitlink。
- `space_preflight.py` 會被 KCC 依絕對檔案路徑獨立載入，不能新增套件相對 import 破壞此介面。
- 跑與變更相關且能驗證行為的測試。快速命令：根目錄執行 `.venv/Scripts/python.exe -B -m unittest discover -s tests -v`；小型 CLI 另加 `-p integration_cli.py`；KCC 從 kcc 目錄以 `../.venv/Scripts/python.exe` 執行同樣的 unittest discover。
- 完整實際驗收使用既有背景腳本及 callback，不輪詢；失敗先診斷，不自動重跑整輪。

## 文件與工作紀錄

- README 面向使用者，docs 保存穩定技術說明，AGENTS.md 保存規則，`.agent/decisions.md` 只保存影響後續工作的決策與原因。
- 只有對外行為、穩定架構或重要決策改變才更新相應文件；每次任務完成不必新增正式工作日誌。
- 執行輸出、冗長除錯與臨時計畫放忽略的 `tmp/` 或 `.agent/scratch/`；不加入個人路徑、thread ID、設定、來源漫畫或成品。舊過程可從 Git 歷史追溯。

## Windows 與環境

- Windows 檔案命令使用原生絕對路徑與可用的 `-LiteralPath`；遞迴刪除／搬移前確認目標仍在預期工作目錄。
- 回覆中的本機連結使用磁碟代號、正斜線與絕對路徑；有空白時以角括號包住目標，不使用 file:// 或 /C:/ 形式。
- 執行任何 uv 命令直接使用 `require_escalated` 申請授權。Python 本體使用使用者層級 uv 預設安裝，專案只建立 `.venv`；授權後仍失敗就回報，不另裝專案 Python、改快取或設定繞過。
