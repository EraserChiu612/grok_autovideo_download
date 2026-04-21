# Grok Auto Video Generator

批次自動化 [grok.com/imagine](https://grok.com/imagine) 影片生成工具。從 Excel 讀取 prompt，逐一生成 MP4 並下載，完成後寫入標記，支援斷點重跑。

---

## 功能

- 從 `prompts/prompts_use.xlsx` 批次讀取 prompt
- 自動登入（支援 X 帳號或 Email 兩種方式）
- Session 持久化，重啟後不需重新登入
- 自動切換影片模式、送出 prompt、等待生成、下載 MP4
- 將 Excel D 欄（主題）、E 欄（註記）寫入 MP4 metadata
- 完成的列在 F 欄標記 `Y`，中途失敗可重新執行補跑
- 使用 [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) 在 binary 層面隱藏自動化特徵，降低 Cloudflare 觸發率

---

## 安裝

```bash
pip install -r requirements.txt
patchright install chromium
```

> **Windows 注意：** 若 `patchright` 指令找不到，使用完整路徑：
> `C:\Users\<用戶名>\AppData\Roaming\Python\Python3xx\Scripts\patchright.exe install chromium`

---

## 設定

複製 `.env.example` 為 `.env` 並填入資訊：

```env
# X 帳號（必填）
X_USERNAME=your_email@example.com
X_PASSWORD=your_password
X_HANDLE=your_x_username        # @ 後面的部分，用於異常驗證

# 登入方式（選填，預設 x）
#   x     → 透過 x.com 登入（標準流程）
#   email → 直接在 accounts.x.ai 用 email 登入
LOGIN_METHOD=email

# CDP 模式（選填，用於連接已開啟的 Chrome）
# 先手動開啟 Chrome：
#   "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome_debug"
# 再取消下行的註解：
#CDP_URL=http://localhost:9222
```

---

## Excel 格式

`prompts/prompts_use.xlsx` 欄位定義：

| 欄 | 內容 | 說明 |
|---|---|---|
| B | Prompt 1 | 第一個影片提示詞 |
| C | Prompt 2 | 第二個影片提示詞（可空） |
| D | 主題 | 寫入 MP4 metadata 標題欄 |
| E | 註記 | 寫入 MP4 metadata 註解欄 |
| F | 完成標記 | 程式自動填入 `Y`，已標記的列會跳過 |

- 每個工作表（Sheet）獨立處理，流水號各自計算
- B、C 都空的列會跳過
- F 欄已有 `Y` 的列跳過（支援補跑）

---

## 使用方式

```bash
python main.py
```

影片儲存至 `output/<日期>/`，檔名格式：

```
output/
└── 20260421/
    ├── Sheet1_01_20260421.mp4
    ├── Sheet1_02_20260421.mp4
    └── Sheet2_01_20260421.mp4
```

---

## Cloudflare 處理

patchright 在 Chrome binary 層面移除自動化標記，多數情況可自動通過 Cloudflare 驗證。首次啟動或 cookie 過期時可能需要手動完成一次驗證，之後 session 會持久保存在 `session/browser_profile/`。

若仍遇到驗證問題，可改用 CDP 模式（見 `.env` 設定）連接手動開啟的 Chrome，完全規避 Cloudflare 偵測。

---

## 專案結構

```
grok_auto_video_project/
├── main.py                    # 主程式：讀取 xlsx、排程、寫入標記
├── grok_automation.py         # 核心自動化：登入、生成、下載
├── requirements.txt
├── .env.example
├── prompts/
│   └── prompts_use.xlsx       # Prompt 來源
├── output/                    # 生成的影片（gitignore）
│   └── <日期>/
│       └── *.mp4
├── session/                   # 瀏覽器 session（gitignore）
│   ├── browser_context.json
│   └── browser_profile/       # cf_clearance 等 cookie
└── docs/
    └── superpowers/
        ├── specs/
        └── plans/
```

---

## 注意事項

- 需要有效的 **X Premium** 訂閱才能使用 grok.com 影片生成
- 每支影片生成約需 30–90 秒，程式最長等待 10 分鐘
- 發生未預期錯誤時，瀏覽器會保持開啟供檢查，按 Enter 後才關閉
- `.env`、`output/`、`session/` 已加入 `.gitignore`
