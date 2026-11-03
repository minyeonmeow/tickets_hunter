# FunOne 下一步與票況更新：HAR 驗證

檢查日期：2026-11-04。依據使用者提供的 funone_jp、funone_smn HAR；
以下為離線檢查結果，未連線購票，也未重送紀錄中的認證或交易請求。

## 活動頁下一步

兩場活動分別使用 `/activity/activity_detail/perfume1114twoman` 與
`/activity/activity_detail/26saymynameintaipei`，下載的 activity_detail 元件完全相同。
活動頁均以 `.round_info.round_info--js` 呈現場次，下一步按鈕呼叫
`GoChooseTicketWaterfall(round)`。這個入口沒有綁定某一場活動。

下一步會依網站流程檢查場次、登入、會員資料、未付款訂單與購票資格等，
再導向 `/purchase/purchase_waiting_jump/<activitySn>/<roundSn>`。
後續才依活動設定進入有座位圖或無座位圖的選票頁。

HAR 中的場次日期分別為 2026/11/14 與 2026/12/13。
程式需開啟 `advanced.funone_auto_next` 及自動選場次，且日期／場次關鍵字符合，
才會自動操作；未符合時不會任意點擊其他場次。

## 即時更新票況

此按鈕不是整頁重整，也不是每點一次就重新呼叫一個取得票況的 HTTP API。

1. 頁面初始化透過 HTTP 取得票區資料，並以 `/ThirdParty/GetSocketToken` 取得連線資訊。
2. 建立既有 WebSocket 連線。JP 紀錄中的主機為 bp21.xin-stars.com；
   SMN 為 bp1.xin-stars.com。主機由網站提供，不應寫死。
3. 按鈕的 `SocketUserGetRoundTicketInfo()` 呼叫
   `ticketGetRoundTicketsInfo(activitySn + "-" + roundSn)`，透過 WebSocket 送出二進位請求。
4. 伺服器回覆 `responseTicketGetRoundTicketsInfo`，前端更新畫面上的剩餘數量。

同一個票況查詢也會用於頁面初始化及網站自動更新，因此 HAR 只能確認訊息及回應；
單憑封包時間不能將每一筆都斷言為人工點擊。

## 已解碼的 JP 回應

WebSocket 訊息為 opcode 2，並非原生 JSON。下列是依 HAR 內網站自己的
request/response packing/unpacking 程式轉換出的可讀表示。
票種名稱與價格由 HTTP `/Ticket/GetArrangeZoneList` 對應，並非二進位回應直接包含。

請求的 `roundSn` 為 `390-1305`，控制碼為 `GET_ROUND_TICKETS_INFO`（0x35）。
回應 `code=1` 表示成功；`zoneList` 包含 zoneSn、salesStartTime、salesEndTime、
availableForPurchase、selected、payment、sold。字串採 UTF-16 LE，長度及計數依網站定義解析。

錄製時間 2026-11-04 02:56（台灣時間）的回應：

| 票種 | zoneSn | 價格 | 可購買 | 已選取 | 付款中 | 已售出 |
|---|---|---:|---:|---:|---:|---:|
| VIP | 3427 | 3000 | 27 | 0 | 0 | 23 |
| 一般票 | 3428 | 1000 | 233 | 0 | 0 | 13 |
| 愛心票 | 3429 | 400 | 3 | 0 | 0 | 1 |

此表是紀錄中的歷史快照，不代表目前庫存或最終可成功購買的數量。
02:56:11.599、02:56:17.153 的同類請求分別於 11.647、17.182 收到回應，數量相同。
收到票況後，前端另呼叫 `/api/ApiFront/GetNowTime` 檢查票種銷售時間；這個 API 回傳時間，
不是票券庫存。

## 冷卻與現有程式行為

網站使用 `get_round_info_flag` Cookie 做約 5 秒冷卻，並以 isUserGetRoundTicketInfo
避免尚未完成時重複更新。因此程式點擊間隔不等於實際 WebSocket 請求頻率。

本次只將自動重整間隔移到基本設定，未新增直連 WebSocket 的查票功能。
程式原有行為仍為優先點擊更新票況按鈕，找不到可用按鈕時可能整頁重整。
