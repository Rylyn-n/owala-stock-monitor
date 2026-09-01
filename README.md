# Owala 補貨監測

GitHub Actions 每 10 分鐘檢查一次商品頁面，狀態從售完變成有貨時透過 LINE 推播通知，
檢查結果寫入 `data/stock_status.json`，由 GitHub Pages 的 `index.html` 讀取顯示。

## 設定步驟

1. 建立 repo，把這些檔案推上去。
2. Settings → Secrets and variables → Actions → New repository secret，新增兩個：
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `LINE_TO`（你的 LINE User ID 或群組 ID）
3. Settings → Actions → General → Workflow permissions，選 **Read and write permissions**。
4. Settings → Pages，Source 選 `main` 分支的 `/ (root)`。
5. Actions 頁面手動跑一次 `庫存檢查`，確認流程正常。

## 檔案

- `monitor.py` — 偵測庫存、發 LINE 通知、更新狀態檔
- `.github/workflows/check_stock.yml` — 排程與 commit 回寫
- `data/stock_status.json` — 目前狀態與歷史紀錄
- `index.html` — 狀態儀表板
