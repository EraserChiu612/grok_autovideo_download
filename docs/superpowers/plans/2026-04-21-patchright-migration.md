# Patchright Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將 playwright + playwright-stealth 替換為 patchright，從 binary 層面移除自動化特徵，提高 Cloudflare 通過率。

**Architecture:** patchright 是 playwright 的 patched fork，API 完全相同，只需更換 import 來源。playwright-stealth 的 JS 注入方式效果有限且 _stealth 物件目前未實際使用，一併移除。

**Tech Stack:** patchright（替換 playwright + playwright-stealth）

---

### Task 1: 更新 requirements.txt

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: 修改 requirements.txt**

將：
```
playwright>=1.58.0
playwright-stealth>=2.0.0
```
改為：
```
patchright>=1.0.0
```

完整 requirements.txt 結果：
```
patchright>=1.0.0
python-dotenv==1.0.1
httpx>=0.27.0
openpyxl>=3.1.0
mutagen>=1.47.0
```

- [ ] **Step 2: 安裝新依賴**

```bash
pip uninstall playwright playwright-stealth -y
pip install patchright
patchright install chromium
```

Expected: 安裝成功，無 error

- [ ] **Step 3: 確認安裝版本**

```bash
pip show patchright
```

Expected: 顯示 patchright 版本資訊

---

### Task 2: 更新 grok_automation.py imports

**Files:**
- Modify: `grok_automation.py:14-26`

- [ ] **Step 1: 替換 playwright import**

將第 14 行：
```python
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Response,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)
```
改為：
```python
from patchright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Response,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)
```

- [ ] **Step 2: 移除 playwright-stealth imports 與初始化**

刪除第 24-26 行：
```python
from playwright_stealth import Stealth

_stealth = Stealth(chrome_runtime=True)
```

- [ ] **Step 3: 確認語法無誤**

```bash
python -c "import grok_automation; print('OK')"
```

Expected: 印出 `OK`，無 ImportError

---

### Task 3: 驗證功能正常

- [ ] **Step 1: 啟動測試執行（headless=False 觀察）**

```bash
python main.py
```

觀察重點：
- 瀏覽器正常啟動（不出現 webdriver 相關錯誤）
- 導航到 grok.com 時不觸發 Cloudflare 驗證（或能自動通過）
- 登入流程正常

- [ ] **Step 2: Commit**

```bash
git add requirements.txt grok_automation.py
git commit -m "feat: migrate from playwright+stealth to patchright for better Cloudflare bypass"
```

---
