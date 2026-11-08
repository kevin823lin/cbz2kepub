# 設定、路徑與作品資料

[README](../README.md#設定) 提供可直接修改的範例，本頁說明完整欄位、路徑及資料處理規則。

| 檔案 | 位置 | 責任 |
|---|---|---|
| `config.toml` | 預設在專案根目錄 | 本批作品、輸入輸出路徑、轉檔與發布設定 |
| `info.json` | 每部作品的 `compressed/` 目錄 | 該作品的作者與簡介，可省略 |

## config.toml

由根目錄的 `config.example.toml` 複製建立個人設定。預設位置不隨啟動工作目錄改變；用 `main.py --config <檔案>` 可選擇另一份設定。`batch`、`paths`、`conversion` 必填，`publication` 可省略；個人設定不納入 Git。

### 漫畫清單與 Ctrl+/

```toml
[batch]
comics = [
    { title = "範例漫畫 A", spreadshift = false, chapter_pairing = true },
    # { title = "範例漫畫 B", spreadshift = true, chapter_pairing = true },
]
```

每部漫畫使用一行 inline table，讓編輯器 Ctrl+/ 能切換整行註解。程式只處理未註解的項目，依清單順序逐部執行；全部註解時不轉檔。一部失敗就停止後續作品。

`title` 是單一作品資料夾名稱，也作為書名、系列名及成品命名來源，不能包含路徑或跳出 `input_root`。`spreadshift` 是必填布林值，控制兩頁配對的起點。

`chapter_pairing` 是選用布林值，省略時為 `false`，使用 KCC 原生配對與裁切。設為 `true` 時，Kobo EPUB 先獨立判斷章首 1／2，確認跨頁後正文從 3／4 開始尋找，否則從 2／3 開始。兩個範圍各自依序嘗試 RGB、嚴格條帶、備援條帶與最外側像素輪廓。

正文決定自己的配對起點，章首跨頁可以形成 `12｜3｜45` 的例外。僅章首命中時採 `12｜34｜56`；章首與正文皆無命中才依 `spreadshift` 排列，`false` 從第 1、2 頁成對，`true` 從第 2、3 頁成對。獨立封面不計入章內頁碼。

每組輸出兩張獨立圖片，縱向逐頁閱讀，橫向依左右頁標記並排。上下裁切按來源高度比例同步，左右沿用各自的裁切範圍。兩張圖片等高、可異寬，以 profile 寬高為上限，直接儲存實際內容。每頁 XHTML viewport 使用該張圖片的尺寸，兩頁保留在同一卷。章內含橫向或正方形圖片時，該章採原生 KCC 處理。辨識與裁切的完整規則見[章內配對](chapter-pairing.md)。

### 路徑與來源

```toml
[paths]
input_root = "D:/Comics"
output_dir = "./output"
work_root = "./.cbz2kepub-work"
```

| 設定 | 意義 |
|---|---|
| `input_root` | 每部作品讀取 `input_root/<title>/compressed/*.cbz` |
| `output_dir` | 所有作品的共用輸出目錄，可獨立於 work_root |
| `work_root` | 可省略。省略時固定使用專案根目錄下的 `.cbz2kepub-work/`。明寫相對路徑時則以設定檔目錄為基準。 |

三者均可為相對或絕對路徑，也可在不同磁碟區。**設定內容中的相對路徑以設定檔目錄為基準**；明確提供的相對 `--config` 路徑則以啟動工作目錄為基準。解析後傳入 KCC 的路徑皆為絕對路徑，因 KCC 啟動腳本會改變工作目錄。

Windows TOML 建議使用正斜線；需要反斜線時可用單引號 literal string。work_root 優先選短路徑及來源所在磁碟區，方便硬連結 CBZ。只把設定改寫成相對路徑，不能縮短實際展開後的路徑。

來源目錄不遞迴；CBZ 須以章號開頭，按十進位排序，例如 24.05 在 24.1 前。空圖片、不安全／重複路徑、加密 archive、缺少來源及未知設定鍵會報錯。

`cover.jpg`、`info.json` 可選，但存在時必須有效；正式轉檔不接受空佔位檔。實際資料驗收工具可將平鋪 CBZ 橋接至標準結構，這項便利不改變正式輸入規則。

### 影像設定

```toml
[conversion]
profile = "KoLC"
manga_style = true
upscale = true
forcecolor = true
eraserainbow = true
cropping = 2
```

| 設定 | 對應 KCC 行為 |
|---|---|
| `profile` | 非空、受支援且具固定解析度的 profile；範例 KoLC 為 1264×1680 |
| `manga_style` | `--manga-style`，由右向左閱讀 |
| `upscale` | `--upscale` |
| `forcecolor` | `--forcecolor` |
| `eraserainbow` | `--eraserainbow` |
| `cropping` | 整數 0 關閉自動裁邊、1 裁邊、2 裁邊與去頁碼 |

表中欄位都必填；布林 false 不傳對應旗標，不接受字串 "false"。影像處理沿用固定版 KCC，不承諾與舊版逐像素相同。

### 發布與備份

```toml
[publication]
keep_backup = true
```

此 table 可省略，預設 true。true 保留舊卷至新卷與所有權清單發布成功，以供回復；成功即刪交易備份，不永久保存歷史版本。false 在所有權與容量預檢通過後，先刪**當前作品**舊卷，重讀實際空間才繼續；失敗無法還原已刪舊卷。

## info.json

放在 `input_root/<title>/compressed/info.json`，採 UTF-8 編碼，內容須為 JSON object。檔案與各欄位皆可省略；已知欄位存在時必須是字串，不能用陣列或 `null`。

```json
{
  "title": "範例漫畫 A",
  "author": "作者甲, 作者乙",
  "description": "作品簡介。\n第二行簡介。"
}
```

| 欄位 | 使用方式 |
|---|---|
| 設定的 `title` | 選取目錄、書名、系列名，寫入 ComicInfo 的 Series／Title |
| `info.json.title` | 存在時須為字串，不取代設定的 title |
| `info.json.author` | 非空時寫入 Writer；作者保留完整字串，不拆人名 |
| `info.json.description` | 非空時寫入 Summary，保留文字及換行 |
| 其他 JSON 欄位 | artist、genre、status 等目前不映射至 EPUB |

wrapper 產生 `ComicInfo.xml`，交由 KCC 原生 metadata 流程讀取。單 CBZ 只覆蓋有提供的欄位，未提供或空作者／簡介保留內嵌值；損壞或空白內嵌 XML 改用有效 wrapper XML。wrapper XML 本身無效仍報錯。Fusion 不合併各章內嵌 metadata。

成品系列位置使用輸出第 1、2、3 卷，不採來源話號。`info.json` 存在但不是有效 object，或上述已知欄位不是字串，都會報錯。

## 輸出與工作區

單卷輸出 `<安全化 title>.kepub.epub`，多卷加數字卷號。檔名清理會預留卷號與副檔名長度，拒絕 Windows 保留名稱、同批清理後碰撞及可能與卷號混淆的作品名。每部作品的 `.cbz2kepub-<識別碼>.json` 記錄其擁有的確切檔案；未知舊檔必須先人工確認並移走，不能按前綴批次刪除。

每次工作在 `work_root/c2k-*` 下建立 `s/`（來源）、`t/`（子程序暫存）、`o/`（KCC 成品）及 `space-plan.json`、`kcc.log`。成功清理整個工作目錄，失敗保留已建立的診斷資料。只為本次子程序設定 TEMP／TMP／TMPDIR，不改全機設定。

轉檔時 KCC 的輸出會即時顯示並寫入 `kcc.log`；耗時階段可能暫時沒有新的進度訊息。

同磁碟區 work 到 output 用 rename；跨磁碟區才複製、比對大小並於成功後刪來源。發布回復、排他鎖及容量限制見 [架構文件](architecture.md)。
