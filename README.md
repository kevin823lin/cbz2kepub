# cbz2kepub

將同一部漫畫的 CBZ 按章節順序轉成 Kobo KEPUB，保留自訂封面、作者、簡介、系列資料與章節導航，並依容量分卷。

## 安裝

目前驗證環境是 Windows、Python 3.13.7。需安裝 `uv`，以及能從 PATH 執行的 7-Zip `7z`。專案使用 KCC fork submodule，從主專案根目錄執行：

```powershell
git submodule update --init --recursive
uv venv --python 3.13 .venv
uv pip sync --python .venv/Scripts/python.exe requirements.lock
```

已經有 `.venv` 時不必重建。程式可直接從 checkout 執行，不需要額外安裝本專案。主專案遠端尚未設定，公開 clone URL 待發布時補齊。

## 準備輸入

使用範例設定時，將作品資料夾放進專案附的 `input/`，轉檔完成後到 `output/` 取書。

```text
input_root/
└─ 範例漫畫 A/
   └─ compressed/
      ├─ 01 - 第1話.cbz
      ├─ 02 - 第2話.cbz
      ├─ cover.jpg       可選
      └─ info.json       可選
```

來源只讀取 `compressed/` 中的 CBZ，不遞迴。檔名須以十進位章號開頭，支援小數章號。

## 設定

### config.toml：選擇作品與轉檔方式

將 [config.example.toml](config.example.toml) 複製為根目錄的 `config.toml`。使用預設資料夾時，只需將漫畫清單中的作品名稱改成你的資料夾名稱。使用其他位置時再調整 `input_root` 與 `output_dir`。已有個人設定時直接編輯。

```toml
[batch]
comics = [
    { title = "範例漫畫 A", spreadshift = false },
    # { title = "範例漫畫 B", spreadshift = true },
]

[paths]
input_root = "./input"
output_dir = "./output"
work_root = "./.cbz2kepub-work"

[conversion]
profile = "KoLC"
manga_style = true
upscale = true
forcecolor = true
eraserainbow = true
cropping = 2

[publication]
keep_backup = true
```

`title` 對應輸入的作品資料夾，也作為成品書名與系列名。每部作品一行，可用編輯器 Ctrl+/ 切換整行註解。 `spreadshift` 調整左右頁配對起點。設定內的相對路徑以 **config.toml 所在目錄** 為基準。省略 `work_root` 時，使用 **專案根目錄** 下的 `.cbz2kepub-work/`。完整欄位與預設行為見 [config.toml 說明](docs/configuration.md#configtoml)。

### info.json：作者與簡介

放在 `input_root/<作品名稱>/compressed/info.json`，使用 UTF-8 編碼的 JSON object：

```json
{
  "title": "範例漫畫 A",
  "author": "作者甲, 作者乙",
  "description": "作品簡介。\n第二行簡介。"
}
```

| 欄位 | 用途 |
|---|---|
| `title` | 可保留來源作品名；成品書名與系列名以 `config.toml` 漫畫清單的 `title` 為準 |
| `author` | 作者完整字串，多位作者可寫在同一字串中 |
| `description` | 作品簡介，以 `\n` 表示換行 |

檔案與各欄位皆可省略；提供上述欄位時，值必須是字串，不能用陣列或 `null`。空值、CBZ 內嵌資料與其他欄位的處理見 [info.json 說明](docs/configuration.md#infojson)。

## 執行

從主專案根目錄執行：

```powershell
.\.venv\Scripts\python.exe .\main.py
# 或指定另一份設定
.\.venv\Scripts\python.exe .\main.py --config .\config.toml
```

預設讀取專案根目錄的 `config.toml`；明確提供的相對 `--config` 路徑以啟動工作目錄為基準。

## 輸出

成品位於 `output_dir`：單卷為 `範例漫畫 A.kepub.epub`，多卷為 `範例漫畫 A 1.kepub.epub` 等。每卷上限 2,000,000,000 bytes；必要時同一章可分至多卷。

程式在 `work_root` 建立短工作區，成功後清除；失敗保留診斷資料。預設保留舊成品至發布成功；`publication.keep_backup = false` 會先刪除本作品的舊卷，後續失敗無法還原。沒有所有權清單的同名舊檔不會直接覆寫。

## 文件

- [設定、路徑與作品資料](docs/configuration.md)
- [程式架構、容量預檢與發布回復](docs/architecture.md)
- [開發、測試與 KCC 維護](docs/contributing.md)
- [驗證範圍與限制](docs/validation.md)

個人設定、漫畫來源、EPUB、虛擬環境與執行日誌不納入 Git。KCC 使用 [維護中的 fork](https://github.com/kevin823lin/kcc)，固定 commit 由 submodule 記錄；Kobo 實機與全新環境重建尚待驗證。
