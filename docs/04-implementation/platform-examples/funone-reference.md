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

- 活動頁由使用者手動進入票區；已移除自動下一步、入口重試與其設定鍵。
- area_auto_select.enable 控制尚未選區時的票區自動選取。已有可用預選時不重新進區。
- refresh_datetime 在 FunOne 購票頁只限制「購買」送出：可於時間到達前準備張數、條款及驗證碼，
  到點後保留原頁與預選，不整頁重整。未設定時間則依網站按鈕可用狀態處理。
- 有指定時間時，依本機時間觸發；GetNowTime 及 monotonic 外推僅供參考，不阻擋操作。
  優先監聽目前分頁的 Network.LoadingFinished 回應；沒有近期樣本時以頁面原生 fetch 補查，
  只回傳成功的公開時間值，不記錄 Token 或完整回應。樣本超過 120 秒失效，補查間隔 20 秒。
- 參考時間差包含秒精度及網路誤差；缺少或過期樣本不影響本機定時觸發。
- 到點後若仍是開賣前畫面，只點 .web_round_info_refresh；尊重 get_round_info_flag、
  進行中的更新、30 秒重試間隔與全域自動重整開關。購買已可用時不額外更新。
- 未到指定時間不進行票況刷新，以免破壞預選；日期格式錯誤時不送出。
- 同輪已確認張數與驗證碼時，不在送出函式重讀一次，減少開賣時的 CDP 往返。
- 網站停用的購買按鈕不強制啟用；已點擊則等待回應，不因逾時重送。
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
也測試隱藏錯誤與大小寫；活動頁入口測試已隨功能移除。
test_scheduled_start.py 測試到點釋放、倒數暫停及其他平台的刷新行為。
2026-11-04 新增 HAR 含活動頁、場次元件與 GetNowTime 回應，
可確認下一步位於 .round_info 內，以及網站使用伺服器時間。
GetNowTime 為 POST /api/ApiFront/GetNowTime，回傳 IsSuccess、Value（台灣時間，精度到秒）；
已加入被動校時及必要的頁面內補查，指定時間依本機時間判斷，網站時間僅供參考，並追蹤網站按鈕狀態。
預選準備與定時購買以本機 Chrome／離線測試驗證，尚未以實際開售活動驗證。
可透過 FUNONE_TEST_CHROME 指定 Chrome 路徑；不連接票務網站。
