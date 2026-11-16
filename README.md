# ✈️ 機票價格追蹤器

每 6 小時到 Google Flights 查一次你指定的航班最低價，畫成走勢圖；價格明顯下降時推播到手機（Android、iPhone 都可以）。
網頁可以「加到主畫面」，用起來就像一個 App：看走勢、新增或刪除要追蹤的航班、看通知紀錄。

- 資料來源：Google Flights（透過 [fast-flights](https://github.com/AWeirdDev/flights) 套件）
- 自動執行：GitHub Actions（免費、電腦不用開）
- 推播：[ntfy](https://ntfy.sh)（不用註冊）或 Telegram
- 網頁：GitHub Pages，網址 `https://<你的帳號>.github.io/flight-price-tracker/`

## 一次性設定

### 1. 手機裝 ntfy，訂閱一個主題
Android 在 Google Play、iPhone 在 App Store 搜「ntfy」。打開 App → ＋ → 輸入一個**別人猜不到的主題名稱**（任何人知道名稱都能看到通知）。

### 2. 把主題名稱存進 GitHub
專案 → **Settings → Secrets and variables → Actions → New repository secret**
- Name：`NTFY_TOPIC`
- Secret：剛剛的主題名稱

（想用 Telegram 的話，再加 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`，兩種可以同時開。）

### 3. 開啟網頁
**Settings → Pages** → Source 選 **Deploy from a branch** → Branch 選 `main`、資料夾 `/docs` → Save。
幾分鐘後打開 `https://<你的帳號>.github.io/flight-price-tracker/`，在手機瀏覽器選「加到主畫面」。

### 4. 讓網頁可以新增／修改航線（選用但建議）
網頁本身沒有伺服器，新增航線時是直接幫你修改這個專案的 `config.json`，所以需要一組只限這個專案用的 GitHub 權杖：
1. 打開 <https://github.com/settings/personal-access-tokens/new>
2. Repository access 選 **Only select repositories** → `flight-price-tracker`
3. Repository permissions：**Contents** 和 **Actions** 都選 **Read and write**
4. 產生後複製，貼到網頁「設定」頁的「存取權杖」，按「儲存並測試」

權杖只存在那支手機的瀏覽器裡，不會上傳到任何地方。沒設定權杖也能看走勢、收通知，只是新增航線要改成直接編輯 `config.json`。

### 5. 測試
網頁「設定」頁按 **傳送測試通知到手機**，約 1 分鐘內 ntfy 會收到；再按 **立即查一次價格**。

## 通知規則

新增或編輯航線時可以開關：

| 規則 | 條件 |
|---|---|
| 創歷史新低 | 比追蹤以來的最低價再便宜 NT$100 以上 |
| 比上次便宜 | 比上一次查到的便宜 3% 以上，或 NT$200 以上 |
| 跌破目標價 | 價格從目標價以上跌到目標價以下 |
| 漲價提醒 | 一次漲 10% 以上 |

連續 3 次抓不到資料也會通知你，恢復後再通知一次。數字門檻可在 `config.json` 微調。
價格是**所有乘客的總價**、經濟艙；來回票是來回總價。

## 檔案

| 檔案 | 用途 |
|---|---|
| `tracker.py` | 抓價、判斷通知、寫入紀錄 |
| `config.json` | 要追蹤的航線與通知規則（網頁會幫你改） |
| `.github/workflows/track.yml` | 每 6 小時執行；`config.json` 被修改時也會立刻執行一次 |
| `docs/index.html` | 網頁介面 |
| `docs/data/history.json`、`alerts.json` | 價格紀錄與通知紀錄（Actions 自動更新） |

本機測試：`pip install -r requirements.txt` 後執行 `python tracker.py --mock`（假資料，不連網）。

## 已知限制

- Google Flights 沒有官方 API，套件是逆向工程，Google 改版時可能暫時失效；可以把 `requirements.txt` 換成 `faster-flights`（程式不用改）或等套件更新。
- GitHub Actions 的機器偶爾會被 Google 擋；一直收到「連續失敗」通知的話，可以改在自己電腦上定時跑 `python tracker.py`。
- GitHub 的排程常延遲幾分鐘到幾十分鐘；網頁更新後 GitHub Pages 也需要 1–3 分鐘才會顯示新資料。
- 實際訂票價格（行李、刷卡手續費等）以航空公司或訂票網站為準。
