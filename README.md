# Grok Auto Video Generator

自動化批次生成 [grok.com/imagine](https://grok.com/imagine) 影片的 Python 工具。

讀取 `prompts/` 資料夾中的 `.txt` 提示詞，逐一生成 MP4 影片並儲存至 `output/`。

---

## 功能

- 自動登入 X（含異常驗證、OAuth 授權）
- Session 儲存，第二次起跳過登入
- 批次處理多個 prompt 檔案
- 自動切換影片模式、填入 prompt、送出
- 等待生成完成後下載 MP4
- 處理完成的 prompt 移至 `prompts/done/`
- 發生錯誤時保持瀏覽器開啟供手動檢查

---

## 安裝

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## 設定

複製 `.env.example` 為 `.env` 並填入 X 帳號資訊：

```env
X_USERNAME=your_email@example.com
X_PASSWORD=your_password
X_HANDLE=your_x_username
```

- `X_USERNAME`：登入用的 Email
- `X_PASSWORD`：X 帳號密碼
- `X_HANDLE`：X 使用者名稱（@ 後面的部分），用於異常登入驗證

---

## 使用方式

1. 在 `prompts/` 資料夾中建立 `.txt` 檔案，每個檔案一個影片提示詞：

   ```
   prompts/
   ├── scene_01.txt   ← "A golden eagle soaring over mountains..."
   └── scene_02.txt   ← "A futuristic city at night..."
   ```

2. 執行：

   ```bash
   python main.py
   ```

3. 影片會儲存至 `output/`，檔名格式為 `<prompt檔名>_<時間戳>.mp4`

---

## 專案結構

```
grok_auto_video_project/
├── main.py              # 主程式入口
├── grok_automation.py   # 核心自動化邏輯
├── requirements.txt     # 依賴套件
├── .env.example         # 環境變數範本
├── prompts/             # 待處理的 prompt 檔案
│   └── done/            # 已處理完成的 prompt
├── output/              # 生成的影片（.gitignore 中排除）
└── session/             # 瀏覽器 session 快取（.gitignore 中排除）
```

---

## 注意事項

- 需要有效的 X Premium 訂閱才能使用 grok.com 影片生成功能
- 每次生成約需 30–90 秒，最長等待 6 分鐘
- `.env`、`session/`、`output/` 已加入 `.gitignore`，不會上傳至 Git
