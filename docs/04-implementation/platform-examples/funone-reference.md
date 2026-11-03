# FunOne 實作參考

更新日期：2026-11-04

程式位置：src/platforms/funone.py。
本次依據為自動配位及自行選位的兩份 HAR，均記錄到 purchase_fill_form
與驗證碼載入，未記錄正式建立訂單及付款成功。
尚未完成真實購票的端對端驗證，不以完成度百分比表示成功率。

## 流程與操作邊界

1. 啟動主程式時依 accounts.funone_session_cookie 注入 Cookie。
2. 活動頁選場次；購票頁依關鍵字、排除條件及選擇順序處理票區。
3. 張數以頁面實際數值確認，不以加號點擊次數推算。
4. 自行選位由使用者選擇；程式讀取網站確認後的 .seat.selected，
   數量等於設定張數才允許繼續。本次沒有新增自動選座演算法。
5. 勾選條款、檢查驗證碼與自動送出設定後點擊購買。
6. 已送出時等待網址／階段變化或明確失敗；逾時不自動重送。
   結果一直不明時，請人工檢查頁面，避免重複交易。
7. 資料確認頁持續處理驗證碼，通知使用者檢查資料並手動送出。
8. 付款及完成頁不自動操作。進入資料頁不代表正式訂單成立。
9. 以瀏覽器 performance.timeOrigin 辨識新文件，F5 與程式重整均重設
   舊頁面的等待／驗證碼狀態。同一文件仍保留防重複送出保護。
   刷新等待期間若文件已變更，交回主迴圈檢查，避免覆蓋新票況。

## 關鍵函式

| 函式 | 用途 |
|---|---|
| nodriver_funone_main | 頁面路由與暫停檢查 |
| _funone_handle_ticket_flow | 選區、張數、條款與送出銜接 |
| _funone_matching_rows | 關鍵字優先順序、空格 AND、排除與回退 |
| nodriver_funone_assign_ticket_number | 逐次調整張數、讀回確認，或等待人工選位 |
| nodriver_funone_click_refresh_button | 僅操作明確的票況更新按鈕 |
| _funone_read_captcha | 區分驗證碼未載入、已載入、部分輸入與可提交輸入 |
| nodriver_funone_captcha_handler | 手動輸入保護、換圖等待、辨識重試 |
| nodriver_funone_ocr_captcha | 依設定載入模型，確認圖片未變更後填入並讀回 |
| _funone_submission_pending | 等待網站結果，明確拒絕時才允許重試 |
| _funone_handle_form | 資料確認頁的驗證碼與人工接手通知 |

## 設定與重試

- date_auto_select.enable、area_auto_select.enable 控制場次／票區自動選取。
- refresh_datetime 設定到點開始；FunOne 活動頁保留原頁面，等目標場次的
  「下一步」可點擊才操作，不強制啟用網站停用的按鈕。未設定時間則立即偵測。
- 場次日期以月／日標準化比對，可接受斜線、點、連字號及月日格式。
  沒有符合關鍵字時會說明原因，不會擅自點選其他場次。
- advanced.funone_entry_retry_interval 控制未跳轉時的下一步重試間隔，預設 0.1 秒，
  可於進階設定調整至 0.1–30 秒。按鈕停用時等待；已跳轉時不再點擊活動頁。
  僅自動關閉明確的「尚未開賣」確認提示，其餘登入／資格視窗留給使用者處理。
  場次已載入時不因等待下一步而重新載入整頁。
- ticket_number、area_auto_fallback、keyword_exclude 限制可選票券。
- ocr_captcha.enable=False 時等待人工填寫，不覆蓋未完成輸入。
- ocr_captcha.force_submit=False 時保留選票結果，等待人工送出。
- OCR 使用共用模型工廠及自訂路徑；無可用模型時回退內建模型。
- 驗證碼長度以輸入框限制為準，未宣告時沿用 4–6 字元檢查。
- 只將顯示中的驗證碼錯誤視為拒絕，忽略隱藏錯誤文字；保留辨識結果大小寫。
- 每輪最多辨識五次，填入成功不重設額度；確認圖片變更後才再辨識。
- 換圖後同一個尚未消失的錯誤不會反覆觸發換圖。暫停後不再換圖、填值或送出。
- 超過次數後，可人工輸入或換圖；部分人工輸入不會被覆蓋。
- 填入成功只代表輸入完成，網站是否接受需依後續回應判定。
- 資料頁通知每次進頁一次，保留自訂通知並補充實際階段。

## 驗證

使用專案 Python 環境執行：

    python -m unittest discover -s tests -p "test_funone*.py" -v

test_funone_flow.py 測試逾時、重複送出、失敗重試與資料頁通知。
test_funone_browser.py 使用真實 Chrome 操作本機 HTML，
測試票區索引、關鍵字、張數、人工選位、刷新與驗證碼生命週期。
也測試下一步按鈕位於場次區塊外、停用按鈕、隱藏錯誤與大小寫。
test_scheduled_start.py 測試到點釋放、倒數暫停及其他平台的刷新行為。
2026-11-04 新增 HAR 含活動頁、場次元件與 GetNowTime 回應，
可確認下一步位於 .round_info 內，以及網站使用伺服器時間。
GetNowTime 為 POST /api/ApiFront/GetNowTime，回傳 IsSuccess、Value（台灣時間，精度到秒）；
本次未新增呼叫、未將其用於校正排程時鐘。排程仍使用本機時間，之後追蹤網站按鈕狀態。
活動入口及重試以本機 Chrome 驗證，尚未以實際開售活動驗證。
可透過 FUNONE_TEST_CHROME 指定 Chrome 路徑；不連接票務網站。
