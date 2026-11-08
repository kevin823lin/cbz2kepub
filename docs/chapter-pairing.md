# 章內配對與共同裁切

每章可保留章首 1／2 跨頁，正文獨立決定雙頁排列。兩張圖片各自保留原生左右裁切，並使用相同的內容高度，供 Kobo 直向逐頁、橫向並排閱讀。實測範圍見[驗證結果](validation.md)。

## 使用方式與相容性

每部作品可設 `chapter_pairing = true`，省略為 false。保留一行 inline table 與 Ctrl+/。
wrapper 傳 `--chapter-pairing`，並用 `CBZ2KEPUB_PAIRING_REPORT` 指定本次 work 內報告。
第一版限 Kobo EPUB，支援 RTL/LTR；啟用時拒絕 noprocessing、onepagelandscape、interpanelcrop、webtoon、stretch 等破壞共同幾何的組合。關閉不變更原生行為。

每個 CBZ 是一章；單 CBZ 的巢狀目錄仍是一章。Fusion 以各 CBZ 頂層目錄分章。
sanitizeTree 後依數字來源 ID 排序。獨立封面不算正文。
章內任一原圖 width >= height，整章 native 回退並記錄，不與其他章混用。
其他章先獨立判斷 1／2，依序嘗試 RGB、嚴格條帶、備援條帶、最外側像素輪廓，任一通過便保留這一組。

| 章首結果 | 正文候選起點 | 正文排列 |
|---|---|---|
| 1／2 通過 | 從 3／4 開始，不檢查 2／3 | 命中 3／4 等奇數頁起點時為 `12｜34｜56`，命中 4／5 等偶數頁起點時為 `12｜3｜45｜67` |
| 1／2 未通過 | 從 2／3 開始 | 命中 2／3 等偶數頁起點時為 `1｜23｜45`，命中 3／4 等奇數頁起點時為 `12｜34｜56` |

正文在自己的候選範圍內獨立完成同一套四層判斷，每層採第一組合格候選，高順位方法優先於較早出現的低順位候選。正文完全無命中而章首有跨頁時採 `12｜34｜56`，兩者皆無命中才使用作品 spreadshift。少於三頁時同樣套用這項回退規則。

章首、正文邊界與章尾的孤頁均 center，不新增空白、不跨章配對。

## 辨識

參考 Mihon Desktop 3d7652e30698508aa0c17798104037708d879b02 的 SharedSpreadMatching。
來源：https://github.com/AltairCardinal/mihon-desktop/blob/3d7652e30698508aa0c17798104037708d879b02/domain/src/commonMain/kotlin/mihon/domain/reader/SharedSpreadMatching.kt
只採邊緣取樣/評分，不移植閱讀器狀態管理。保留相應 Apache-2.0 授權和歸屬。

50 個高度樣本，y=floor(i*height/50)；左 x=min(2,width-1)，右 x=max(0,width-3)。
RTL 比較前頁左側、後頁右側；LTR 反向。透明像素以白底合成。
平均每點 RGB 絕對差之和 <30。兩邊皆有 >=80% 樣本的各 channel >220 則拒絕；
兩邊皆有 >=80% 樣本的各 channel <50 則拒絕；
兩邊灰階亮度 variance 均 <100 則拒絕。亮度權重 .299/.587/.114。
條帶取樣沿用相同閱讀方向。將接縫兩側各 8 像素寬的區域轉成白底灰階，以 BOX 縮放各取 50 段平均亮度。

- 若兩側各有至少 40 段亮度高於 220，或各有至少 40 段低於 50，排除該頁對。
- 至少 15 段的兩側亮度都須落在 20 到 235 之間。這些段落兩側的亮度變異數各須至少 100，避免僅因頁框和留白相似而配對。

通過上述條件後，同一份亮度樣本依下表評分。在當次候選範圍找不到 RGB 或嚴格條帶候選時，才採用備援條帶。

| 條帶規則 | 全高 Pearson 相關係數下限 | 全高平均亮度絕對差上限 | 有效段落相關係數下限 |
|---|---:|---:|---:|
| 嚴格 | 0.93 | 16 | 0.80 |
| 備援 | 0.82 | 25 | 0.60 |

當接縫穿過斜線或深色輪廓時，8 像素條帶可能涵蓋兩側不同的圖案。最後一層只取緊鄰分界的一列像素，沿高度取 50 段平均亮度。兩頁亮度的相關係數須達 0.95，平均絕對差最多 16；相鄰段落亮度變化的相關係數須達 0.85，兩側的亮度與變化量各自的變異數均須達 100。

為避免兩頁只因同為黑邊就配對，至少四處須同時變亮或同時變暗，且兩側變化幅度都達 10。首末兩處須相距至少 12 段，這些變化的方向須至少反轉兩次。純色邊、單一步階或單一平直色帶無法單靠亮度吻合通過。

掃描原圖時可以暫存最外側輪廓候選。只有當次候選範圍的 RGB、嚴格條帶與備援條帶都未命中，才會採用該候選。章首與正文各自判斷，章首較低順位的命中可以與正文較高順位的命中並存。日誌分別列出章首與正文的命中頁碼、方法、分數及最終排列。這些分數都不表示辨識準確率。
使用與原生圖片處理一致的來源方向，不另加入 EXIF 自動旋轉功能。

## 共同幾何

重用原生裁切候選與首張彩色豁免、裁切幅度/面積限制，左右保留各自結果，包括內側。
共同上界=min(top1/h1,top2/h2)，共同下界=max(bottom1/h1,bottom2/h2)。
映回原圖 top=floor、bottom=ceil，向外取整；左右同樣不得因取整越界。
不同高度透過比例座標換算，不先縮放整張圖片。
裁後 ri=widthi/heighti；共同內容高度=floor(min(H,W/max(r1,r2)))。
upscale=false 再限制共同高度不超過任一裁後高度。各頁內容寬=floor(ri*共同高度)，至少1像素。
任一頁縮小用 LANCZOS，否則 BICUBIC。共同處理頁不再進單頁 fit/contain 或 splitCheck。
原生 gamma/contrast/color/eraserainbow 逐頁保留。濾鏡完成後直接編碼成實際內容尺寸，JPEG 的寬高同時用作該頁 XHTML 的 viewport。profile 的 W×H 是縮放上限，兩個成對頁可以異寬，但內容高度一致。
RTL 第一頁標為右頁、第二頁標為左頁，LTR 相反。閱讀器依頁側標記組合跨頁。純直頁章的孤頁置中，以等比例 contain 縮放至 profile 範圍內。含寬圖或正方形圖片的章節維持 KCC 原生的裁切、拆頁、旋轉與縮放。
每個 pair worker 順序處理兩頁，避免同時持有兩套大型濾鏡陣列；沿用現有 worker 數。

## 配對報告 v3

成對頁必須滿足 `image_size == viewport == geometry.rendered_size`，且同組兩頁等高。v3 分別記錄章首與正文的證據。wrapper 仍可核對 v2 的整章單基準報告。v1 記錄固定畫布內的內容位置，重新核對歷史 v1 報告時請使用產生該報告的版本。

JSON 由 parent 寫入，worker 不寫共享檔；檔案以暫存後 replace 更新。
頂層：
- version: 3
- state: "analyzing" 或 "completed"；只有所有卷完成後才能 completed
- target: [W,H]，表示輸出尺寸上限
- direction: "rtl" 或 "ltr"
- chapters: 章節陣列
- groups: 組陣列
- pages: 實際正文圖片輸出紀錄陣列

chapters 每筆：
- id: 從1開始章序；title: 導航/來源章名
- source_ids: 本章有序整數 ID 清單
- mode: "paired" 或 "native"
- spreadshift: 正文以原章頁碼計算的起點奇偶，偶數頁起配為 true。僅章首命中時為 false，完全無命中與 native 則沿用作品設定。
- reason: "detected"（正文命中）、"opening"（僅章首命中）、"spreadshift"（完全無命中）、"wide_image"。
- opening_anchor: 章首 1／2 命中證據或 null。native 必為 null。
- anchor: 正文命中證據或 null，章首有命中時正文證據最早可為 3／4，章首未命中時最早為 2／3。

兩種證據皆使用 {"source_ids":[前ID,後ID],"score":number}。RGB 候選沿用原格式，score 為平均 RGB 距離。`strip`（嚴格條帶）、`strip_fallback`（備援條帶）及 `edge_profile`（最外側像素輪廓）均以全高亮度相關係數作為 score。

groups 每筆：
- id: 從1開始整本唯一組ID；chapter_id: 章ID
- source_ids: 有序整數清單；paired為2、single/native為1
- mode: "paired"、"single"、"native"

pages 每筆（依最終閱讀順序；native 同一 source_id 可以多個資源）：
- source_id: 整數；group_id: 組ID
- image: EPUB ZIP 內圖片的 POSIX 路徑
- xhtml: EPUB ZIP 內正文 XHTML POSIX 路徑
- tome: 從1開始卷號
- side: "left"、"right"、"center"（最終實際 spine）
- image_size: [w,h]；viewport: [w,h] 或 null（僅 native 可缺）
- geometry: paired 為 {"crop_box":[l,t,r,b],"rendered_size":[w,h]}，其他 null

不把 worker 回呼完成順序當閱讀順序；全局來源ID保持既有命名。
組/章內不得跨 source ID 範圍、不重複、不跳頁。native 拆頁保留一對多。
最終報告比對 EPUB，不以報告自我宣稱取代像素/ZIP驗證。
helper 完成封面與系列資料後套配對章頁側，native 章不覆蓋。封面不列 pages。

## 分卷、驗證與容量

分卷保護每組 paired，不僅 anchor；兩條逐頁分卷路徑和原生 above/below 都需保護。
整組超過 targetSize 明確報錯。目錄改短或攤平不影響來源ID。
wrapper 保留 CRC/XML/資源/章節/metadata/封面/正文來源ID與順序驗證。
另外核對報告 completed、章來源範圍、實際相鄰／同卷／頁側與成品圖片尺寸。每個孤頁組只對應一個置中頁，其 viewport 等於該張圖片尺寸。

calibrate_images 增加 keyword chapter_pairing=False；cache key包含此值。
仍使用既有分層抽樣、25%樣本餘裕及32MiB操作餘裕，不新增全域倍數。
配對抽樣將等比例縮放後的內容直接試編碼。寬圖章維持原生模擬。初始抽樣是容量近似值。
不計RAM、不預留合圖/額外來源副本/第二份EPUB；處理後仍依實際 bytes 重檢。
space_preflight.py 仍可按絕對檔案獨立載入，不新增套件相對 import。

## 驗收

測試：原設定相容、兩種基準、early-stop、無候選、RTL/LTR、不同高度、crop0/1/2、
upscale=false、合成跨頁線條、寬圖回退、孤頁、兩條分卷路徑、超大pair拒絕、實際報告/資源比對。
小型 CLI 驗證單CBZ巢狀與Fusion、metadata/cover、來源SHA不變。
小批開關交錯三輪比較總/圖片/偵測時間與程序RSS合計峰值，獨立tmp輸出。
再用既有背景腳本執行70章；callback驗收，失敗先診斷、不自動重跑。
實機未測不得宣稱通過。KCC驗證/提交/推送後才更新主專案gitlink；v12.1.4不動。

## 限制

即使章內配對正確、圖片等高且保留完整接縫，Kobo 橫向並排仍可能顯示細縫。第 2 話實測與暫不採用合圖的決定見[驗證結果](validation.md)。

章首誤判會影響是否檢查 2／3；正文首組誤判會影響正文基準。不辨識正文中途改變配對基準。
不同高度按比例對齊不代表原始掃描物理尺度相同。獨立內側裁切可能破壞接縫。
不統一亮度/色調、不修復缺圖或掃描錯位。不新增GUI、來源sidecar、永久辨識快取。

