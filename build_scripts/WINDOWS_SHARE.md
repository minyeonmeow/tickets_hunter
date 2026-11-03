# 一鍵打包 Windows 分享版

## 製作者

在專案根目錄雙擊 **build-windows.cmd**。

腳本會選用 Python 3.10／3.11 的 64 位元環境，自動安裝固定版本的依賴及
PyInstaller，再產生兩個 EXE、乾淨設定、雙擊啟動檔、使用說明及對應原始碼。
製作者電腦需要 Python；收件者不需要。

每次使用全新的 build/windows-share-* 工作目錄和 dist/TicketsHunter_Windows_*
輸出資料夾，不刪除、覆蓋先前版本。

成功後將顯示以下路徑：

- dist/TicketsHunter_Windows_日期時間.zip：交給朋友的檔案。
- 同名資料夾：可直接執行，也可先自行測試。
- 同名 .zip.sha256：檔案完整性雜湊。

## 收件者

1. 將 ZIP 完整解壓縮到桌面等可寫入的位置。
2. 雙擊 **Start_Tickets_Hunter.cmd**，也可以直接雙擊 **settings.exe**。
3. 瀏覽器會開啟設定介面，設定活動及選票條件後按開始。

不用安裝 Python 或程式套件。請保留整個資料夾，尤其是 _internal，
不能只傳單一 EXE。此版本供 Windows 64 位元使用；購票需要網路及可用 Chrome。

## 分享內容與驗證

- 設定一律由程式的預設設定工廠產生，不複製 src/settings.json。
- 不包含帳號設定檔、Cookie、瀏覽器使用者資料、執行紀錄或 HAR。
- EXE 啟動、內建與通用 OCR、設定網頁及全域重整開關都必須通過檢查，
  才會產生分享 ZIP；測試結束會還原乾淨設定。
- 測試 EXE 時移除 Python 路徑依賴；不會連線售票網站或送出購票。
- Source.zip 附上對應原始碼與打包腳本，LICENSE.txt 保留專案授權。
- 已使用的資料夾可能產生帳號與 Cookie；再次分享請重新打包。

已有相依套件的開發者可以直接執行：

    .venv\Scripts\python.exe build_scripts\build_windows_share.py --skip-install

若電腦有設定 PYTHONHOME／PYTHONPATH，請使用 build-windows.cmd；它會先清除
本次執行的干擾變數，不修改系統設定。
