# 架構與安全邊界

## 模組與入口

```text
main.py
  └─ cbz2kepub.converter.main()
       ├─ 設定、來源檢查、ComicInfo.xml、KCC CLI
       ├─ space_preflight：容量計畫與階段檢查
       ├─ epub_outputs：成品驗證與交易發布
       └─ fat32_filename：成品檔名相容性
```

自有 Python 模組集中在 `cbz2kepub/`；根目錄只保留啟動用 `main.py`。此為可直接從 checkout 執行的套件目錄，不需 editable install。第三方依賴安裝至 `.venv`，KCC 保持根目錄的獨立 submodule。

converter 的專案根路徑由模組檔案位置推算，供預設 config、KCC 腳本及範例定位。使用者路徑則按設定檔位置解析，不能混用啟動工作目錄。

`config.toml` 與 `info.json` 的格式、位置及欄位規則見 [設定文件](configuration.md)。

來源參數超過 Windows 命令列長度時明確報錯，不靜默分批而改變 Fusion 結果。

## 轉檔流程

1. 讀取設定、依十進位章號排序 CBZ，檢查 archive 邊界與作品資料。
2. 鎖定 output，估算整批各磁碟區峰值；試建硬連結確認實際複製需求。
3. 建立本次 `c2k-*` 工作區；CBZ 連結至 s，不支援才於容量檢查後複製，封面副本放 `s/Covers/`，並產生 ComicInfo XML。
4. 用專案 Python 執行 `kcc/kcc-c2e.py`。單 CBZ 直接處理，多 CBZ 使用原生 Fusion；fused 搬入同磁碟區短工作目錄，避免 copytree。
5. KCC 處理圖片與分卷；保留完整章名的導航對照。過長內部目錄改短 ID，仍超過 220 字元時報錯，不攤平章節。
6. KCC 在第一次 ZIP 前完成封面 XHTML、第一個置中 spine 與系列卷序；wrapper 不解包重寫整本 EPUB。
7. wrapper 驗證所有卷的 ZIP CRC、XML、資源、正文來源 ID、順序、章節導航及封面，再依數字卷號命名並發布。

每卷成品硬上限 2,000,000,000 bytes，KCC 分卷目標 1850 MiB。超大章可按不可拆單位續卷，包括原生 above／below 圖片和章內配對的兩個正文頁。單一單位超過目標時報錯。

啟用章內配對時，KCC 在數字來源 ID 確定後逐章分析原圖，章首 1／2 與正文各自完成跨頁辨識。worker 依最終分組取得兩頁的原生裁切候選，以共同垂直範圍和內容高度分別編碼。XHTML 以各張圖片的實際寬高宣告 viewport。主程序依最終 spine 順序寫入 work 內的 v3 `pairing.json`，分別保存章首與正文證據。wrapper 在發布前以各卷的實際圖片標頭、XHTML viewport、來源 ID 及左右頁屬性交叉核對。規格與欄位見[章內配對與共同裁切](chapter-pairing.md)。

## KCC 維護介面

KCC 的實作版本由主專案 gitlink 指定。`v12.1.4` tag 保留為原始基準。

| KCC 接點 | 本專案用途 |
|---|---|
| `main()/getWorkFolder()` | owned fusion 同磁碟區消耗，以及選用容量計畫 |
| `sanitizeTree()/chunk_directory()/chunk_process()` | 小數頁序、短路徑、保留章節分卷 |
| `makeFusion()/makeBook()` | 帶入整本 ComicInfo；單 CBZ 覆蓋提供的欄位 |
| `getMetadata()` | 原生 metadata 讀取，維持上游函式 |
| `buildEPUB()` | 封裝前呼叫 `epub_helpers.finalize_epub()` |
| `chapter_pairing`、`imgPairProcessing()` | 選用的章內基準、共同幾何與兩頁工作單位 |
| 圖片處理與 ZIP 交界 | 呼叫容量計畫的階段預檢 |

原生 metadata.py 維持不變；wrapper 省略空值，並補償固定版 parser 額外的 unescape。空／損壞內嵌 XML 回退至 wrapper XML，不吞掉無效 wrapper XML。

wrapper 透過兩個絕對路徑環境變數交付容量介面：

- `CBZ2KEPUB_SPACE_PLAN`：本次工作計畫 JSON。
- `CBZ2KEPUB_SPACE_MODULE`：套件內 `space_preflight.py`。

KCC 明確載入該檔案；此模組須能獨立載入，不能依賴套件相對 import。未提供計畫時，原生 CLI／GUI 的容量檢查不變。

## 空間生命週期

每個實際 volume 個別計算「同時存在的新增占用」最大值；既有來源、舊成品與失敗殘留已反映在 free，不再重複加算。磁碟區由實際路徑的 `st_dev` 識別，不能只比較磁碟代號。

| 階段 | 計入與釋放 |
|---|---|
| staging | 硬連結不計內容副本；copy 的 CBZ 留至成功清理 |
| 解壓 | 依 ZIP 未壓縮 bytes，包含會解出的非圖片資料 |
| 圖片處理 | 尚未刪的原圖、已寫新圖及 worker 同時寫入的重疊 |
| 分卷 | 同磁碟區搬移，不新增一份圖片 |
| 封裝 | 本卷工作檔與新 ZIP 共存；完成後刪本卷工作檔，前卷 ZIP 繼續保留 |
| 發布 | 同 volume rename 不新增成品副本；跨 volume 的 output 需容納副本 |
| 下一部作品 | work 釋放，前面作品的 output 持續占用 |

整批開始、每部開始、copy 前、解壓前、圖片前後、每卷 ZIP 前與發布前會重讀 free。圖片與分卷完成後改用實際大小／卷數重算剩餘需求；重檢只算當下之後的增量。

初始估計讀所有圖片尺寸標頭，依原生預設拆頁門檻分組，再按來源大小分成等量區段，最多取 24 張在 RAM 試編碼 JPEG。各樣本只代表所屬區段，輸出加 25% 估計餘裕；圖片處理前沿用這些分層預測，不改回全域最大頁面乘頁數。試編碼採 profile 解析度、JPEG 品質、upscale、forcecolor 與 eraserainbow 設定，依長寬比選擇 contain／fit，仍未完整模擬裁邊、gamma 與自動對比。這不是未見圖片的上界，也不將解碼像素緩衝當成磁碟需求。

EPUB 固定使用標準庫 ZIP_STORED，先寫正確的 mimetype，再串流加入其他檔案；不執行 7z 內部改名，也不預留第二份 archive。7z 仍用於來源解壓與原生非 EPUB 封裝。圖片完成後改用實際分卷圖片 bytes、各卷封面在 RAM 編碼的大小及文件預算；每卷 ZIP 前再按完整工作檔與 ZIP 結構檢查新增需求。

各階段操作餘裕 32 MiB；整批時間線已含，不再重加。刪舊檔前的 journal 預留 4 KiB；不備份模式可用刪舊卷釋放的空間滿足後續餘裕，但刪後須確認實際 free。其他程序仍可占用空間，預檢不是容量預留。

初始與逐 CBZ 檢查共用解壓排除規則，跳過 __MACOSX、.DS_Store、thumbs.db、Thumbs.db 路徑；`._*` 會先解出再清理，所以計入解壓 bytes、不算正文。

## 發布與回復

output 中每部作品的所有權清單列出確切卷檔；名稱相同但無清單、清單不完整或作品身分不符時拒絕覆寫。`.cbz2kepub.lock` 讓同一輸出目錄一次只有一批工作。

發布先記錄 `.cbz2kepub-publish-*/transaction.json`，再將新卷搬移／複製至 incoming，保留舊卷至 backup，最後安裝新卷及 manifest。跨磁碟副本比對大小；所有安裝成功後才移除 work 原件及交易備份。

一般 I/O 例外會還原新卷來源與保留的舊卷。回復失敗或發布途中 Ctrl+C／SystemExit 時，保留交易、新舊資料與 journal，阻擋下一次同作品發布；中斷可能出現在 rename 成功但完成清單尚未更新的空窗，不能據該清單直接刪 backup。

`keep_backup=false` 在預檢通過後先 journal 化並刪當前作品的舊卷；後續失敗無法還原已刪資料。這個選項不改變所有權檢查。

程序中斷後，先確認沒有轉檔程序，再保留整個交易與工作目錄，按 journal 的 old_files、new_files、sources 及 backup／incoming／output 實際內容回復。確認完整後才移除交易目錄與殘留鎖；不要按檔名前綴批次清理。多檔發布不宣稱整批原子完成。
