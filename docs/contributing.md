# 開發與維護

## 環境

自有程式在 `cbz2kepub/`，使用者入口為根目錄 `main.py`。KCC 是獨立的 Git submodule。

從主專案根目錄取得固定 submodule 與依賴：

```powershell
git submodule update --init --recursive
uv venv --python 3.13 .venv
uv pip sync --python .venv/Scripts/python.exe requirements.lock
```

已有 .venv 不必重建。Python 本體使用使用者層級 uv 預設安裝，專案只建立 .venv。透過 Agent 執行 uv 時遵守本機授權流程；權限失敗不另裝 Python 或改快取繞過。

`requirements.txt` 引用 `kcc/requirements.txt`，lock 以 Windows／Python 3.13.7 編譯，其他平台需另外解析與驗證。更新依賴時：

```powershell
uv pip compile --python .venv/Scripts/python.exe requirements.txt --output-file requirements.lock
uv pip sync --python .venv/Scripts/python.exe requirements.lock
```

不需要 editable install；7z 必須可從 PATH 使用。

## 測試

在主專案根目錄執行：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p integration_cli.py -v
```

KCC 測試從 `kcc/` 執行：

```powershell
..\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_*.py' -v
```

只跑與修改相關的檢查；模組搬移須核對 import、mock 目標、專案根目錄、外部工作目錄啟動與 KCC 容量模組載入。合成 CLI 會處理圖片並啟動 KCC，不放進一般快速測試的命名範圍。

完整實際資料驗收使用 `scripts/run-acceptance.ps1`，必填 SourceDir、ThreadId，可指定 ResultsRoot 與 Limits。Limits 預設 15／59／60／70，`-Limits 70` 只跑 70 章。SourceDir 可指向作品的 `compressed` 目錄或平鋪 CBZ 的作品目錄。工具建立獨立輸入與成品，啟用章內配對，將驗證過的報告保留在各 case 中。驗收副本會略過 0-byte 可選封面／JSON，正式轉檔仍會檢查來源。

腳本在完成／失敗後 callback；等待期間不輪詢。失敗先讀 acceptance.json、acceptance.log 診斷，不自動重跑整輪。完整日誌、成品、封面與逐章對照留在忽略的 tmp，不加入公開文件。

比較配對的時間與記憶體時，在 `tmp/sample.json` 指定代表章節。每個 `file` 是來源目錄內的 CBZ 檔名。

```json
{"source_dir":"D:/Comics/範例漫畫/compressed","title":"範例漫畫","selected":[{"file":"01 - 第1話.cbz"},{"file":"02 - 第2話.cbz"}]}
```

將 `source_dir` 換為實際目錄後，在專案根目錄執行以下命令。

```powershell
.\.venv\Scripts\python.exe -B scripts/benchmark_pairing.py --sample tmp/sample.json --results-root tmp/pairing-benchmark
```

使用新的或空的結果目錄，並選擇能與來源建立硬連結的磁碟區。工具先暖機兩次，再交錯量測三輪，在每次執行的目錄保留成品與日誌。`benchmark.json` 記錄總轉檔時間、KCC 階段耗時、來源雜湊與封面檢查。RSS 是每 100 ms 取樣的程序樹 RSS 總和，包含共用頁面，短暫峰值也可能落在取樣間隔內。

## KCC fork 與 submodule

主專案以 gitlink 固定 [kevin823lin/kcc](https://github.com/kevin823lin/kcc) 的版本。`v12.1.4` tag 保留在原始基準 `370d58d`。

更新時先在 fork 合併上游、解決衝突並跑相關驗證，再提交／推送 KCC，最後在主專案提交新的 gitlink。一般安裝不加 `--remote`，避免跳過固定版本。有未提交修改時先保存工作。

KCC 修改保持小範圍接點，wrapper、設定與整體發布留在主專案；不另維護同內容 patch 副本。metadata.py 維持原生，影像流程仍用 Pillow。詳細責任見 [architecture.md](architecture.md)。

## 文件與公開內容

- README 只放使用者開始使用所需資訊。
- docs 放穩定架構、設定、開發與驗證範圍。
- AGENTS.md 放開發規則與文件索引；.agent/decisions.md 記重要決策及原因。
- .agent/scratch/ 與 tmp 保存本機草稿／執行證據，不提交。過程記錄可由 Git 歷史追溯，不重複貼入正式文件。
- 對外行為或穩定設計改變才更新正式文件；不因每次完成任務就新增工作日誌。

公開倉庫僅納入程式、範例與可公開合成測試；不加入個人 config、來源漫畫、info.json、封面、EPUB、虛擬環境、私人路徑或通知 thread ID。

KCC v12.1.4 採 ISC，保留其版權及授權聲明；主專案的最終授權尚未決定。散布 Python／外部工具／打包檔時另核對所包含依賴的授權。

## 發布前尚需完成

1. 決定主專案遠端與授權，從可取得的乾淨 clone 重建固定環境。
2. 完成 Kobo 實機閱讀、傳書後 metadata 與全面畫質驗收。
3. 若提供 GitHub source ZIP，說明它不含完整 submodule；若提供完整 release 包，再設計其打包流程。

目前沒有新增自動打包系統；驗證狀態以 [validation.md](validation.md) 為準。
