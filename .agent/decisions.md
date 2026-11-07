# 持久設計決策

本檔只保存後續修改需要知道的原因；操作方式以 docs 為準，不追加一般任務日誌。

- **章內配對。** 一個來源 CBZ 採一種配對基準。第一組通過接縫評分的頁對即可決定章首奇偶；其餘頁對共同計算垂直裁切與內容高度，讓未再辨識的跨頁也能對齊。左右裁切保留各頁原生候選，接受可能裁掉接縫的取捨。以來源 ID 貫穿 worker、分卷及 spine，避免依賴暫存檔名或平行工作的完成順序。見[章內配對規格](../docs/chapter-pairing.md)。

- **根目錄入口與套件：** 使用 `main.py` 啟動 `cbz2kepub.converter.main()`，自有程式集中在套件，第三方依賴留在 `.venv`。目前用途是從 checkout 轉檔，採根目錄套件即可，不增加 editable install、打包後端或多個 CLI。
- **KCC fork 加 submodule：** fork 保存必要修改，主專案固定 commit，避免安裝時重複套 patch。基底 v12.1.4 tag 不移動；合併上游後先驗證／推送 KCC，再更新主專案指標。兩者各自管理版本，不把 wrapper 混進 KCC。
- **沿用 ComicInfo 與原生 reader：** wrapper 本來就能從設定／info.json 產生 ComicInfo XML，不新增 metadata CLI／JSON 通道。固定版 parser 額外 unescape 的相容處理由產生端補償，空值省略；不要擴大改寫 metadata.py。單檔缺欄位保留有效內嵌值，損壞內嵌 XML 才回退 wrapper XML。
- **封裝前補齊 EPUB：** 封面閱讀頁、第一個置中 spine 與系列卷序由 KCC buildEPUB 收尾 helper 完成。EPUB 使用標準庫 ZIP_STORED，先寫正確的 mimetype，避免 7z 改名造成整份 archive 暫存與重寫；容量模型只計正在建立的一份 ZIP。wrapper 只讀驗證，不恢復整本 EPUB 解包重寫。
- **短路徑與章節：** 過長內部章節目錄使用短 ID，完整標題保留在導航對照；不要為路徑過長攤平整本書。保留十進位小數頁序、章節分卷與續章配對；仍不支援的路徑明確失敗。
- **容量按生命週期：** 以實際 volume 的同時新增峰值計算，硬連結不重複計內容，已存在檔案不再次加入剩餘需求。JPEG 以拆頁類型及來源大小分層加權，保留各圖片預測至處理前，不把最大樣本套到全部頁面。抽樣仍是估計，處理完成後改用實際大小；不能把原生倍率當作全流程保證。
- **發布與中斷：** 同 volume rename，跨 volume 才複製。以 manifest 限定所有權，不按前綴刪檔。rename 成功與完成清單更新之間可被中斷，所以 Ctrl+C／SystemExit 保留交易與資料待確認，不依不完整清單刪 backup。keep_backup=false 是明確放棄已刪舊卷的回復，不可因容量不足自動切換。
