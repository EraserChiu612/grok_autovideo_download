"""
grok_automation.py
核心自動化邏輯：登入 X、操作 grok.com/imagine 影片生成、下載影片
"""
import asyncio
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from patchright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Response,
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 設定區：若 grok.com UI 更新，只需修改此區塊
# ──────────────────────────────────────────────
GROK_IMAGINE_URL = "https://grok.com/imagine"
SESSION_FILE = "session/browser_context.json"
# 持久瀏覽器個人資料目錄（存放 cf_clearance 等 Cookie，避免每次觸發 Cloudflare）
BROWSER_PROFILE_DIR = "session/browser_profile"

SELECTORS = {
    # ── 登入相關 ──
    # grok.com 繁中介面：登入為 <a> 連結
    "sign_in_btn":      'a[href*="sign-in"], a:has-text("登入"), button:has-text("登入"), a:has-text("Sign in")',

    # X 登入表單（在 x.com 上，介面為英文）
    "x_username_input": 'input[name="text"], input[autocomplete="username"]',
    "x_next_btn":       'button:has-text("Next"), div[role="button"]:has-text("Next")',
    "x_password_input": 'input[name="password"], input[type="password"]',
    "x_login_btn":      'button:has-text("Log in"), div[role="button"]:has-text("Log in")',

    # ── Cookie 同意框 ──
    "cookie_accept_btns": [
        'button:has-text("接受所有 Cookie")',
        'button:has-text("全部允許")',
        'button:has-text("Accept all cookies")',
        'button:has-text("Accept All")',
        '#onetrust-accept-btn-handler',
    ],

    # ── 範例框 / 歡迎彈窗 關閉按鈕 ──
    "modal_close_btns": [
        'button[aria-label="Close"]',
        'button[aria-label="關閉"]',
        'button[aria-label="Dismiss"]',
        'button:has-text("Close")',
        'button:has-text("關閉")',
        'button:has-text("Got it")',
        '[data-testid="modal-close-button"]',
        'div[role="dialog"] button',
    ],

    # ── Imagine 頁面操作 ──
    # 影片模式：radiogroup 內的 radio "影片"（<button role="radio"> 結構）
    "video_mode_btn":   'button[role="radio"]:has-text("影片"), [role="radio"]:has-text("影片")',
    # prompt 輸入框：contenteditable div 或 role=textbox
    "prompt_input":     '[contenteditable="true"], [contenteditable=""], textarea, [role="textbox"], p[contenteditable], div[contenteditable]',
    # 送出按鈕：實際文字是「送出」
    "submit_btn":       'button:has-text("送出"), button[aria-label="送出"], button[aria-label="Send"], button[type="submit"]',

    # ── 圖片附加 ──
    "attach_btn":       'button:has-text("附加"), button[aria-label="附加"]',
    "animate_menuitem": '[role="menuitem"]:has-text("動畫圖像"), menuitem:has-text("動畫圖像")',

    # ── 影片設定（時長 / 比例）──
    # 設定按鈕：有些 UI 需先展開設定面板
    "settings_btn":     'button[aria-label*="設定"], button[aria-label*="Settings"], '
                        'button[aria-label*="options" i], button:has-text("設定")',
    # 時長選項
    "duration_10s":     '[role="option"]:has-text("10"), [role="radio"]:has-text("10"), '
                        'button:has-text("10秒"), button:has-text("10 秒"), button:has-text("10s"), '
                        '[data-value="10"], li:has-text("10秒"), li:has-text("10s")',
    # 比例：先點擊「長寬比」按鈕展開選單，再選 9:16
    "aspect_ratio_btn": 'button[aria-label="長寬比"], button[aria-label="Aspect ratio"], '
                        'button[aria-label*="ratio" i]',
    "aspect_9_16":      'button:has-text("9:16"), [role="option"]:has-text("9:16"), '
                        '[role="menuitem"]:has-text("9:16"), li:has-text("9:16"), '
                        'button[aria-label="9:16"], [role="radio"]:has-text("9:16")',

    # ── 生成完成後 ──
    "video_element":    'video[src], video source[src]',
    "download_btn":     (
        'a[download], '
        'button:has-text("下載"), button:has-text("Download"), '
        'a:has-text("下載"), a:has-text("Download"), '
        'button[aria-label*="下載"], button[aria-label*="download" i], '
        'a[aria-label*="下載"], a[aria-label*="download" i], '
        '[data-testid*="download" i]'
    ),
    "loading_indicator":'[aria-label*="loading" i], [data-testid*="loading"], .loading, [class*="spinner"]',

    # ── 影片畫質 ──
    "resolution_hd": (
        'button:has-text("720p"), [role="radio"]:has-text("720p"), '
        '[role="option"]:has-text("720p"), button:has-text("HD"), '
        '[role="radio"]:has-text("HD"), button:has-text("高畫質"), '
        '[data-value="720p"], [data-value="hd"]'
    ),
}

TIMEOUTS = {
    "login":      30_000,   # 30 秒
    "navigation": 20_000,   # 20 秒
    "generation": 600_000,  # 10 分鐘（影片生成最長等待）
    "download":   60_000,   # 1 分鐘
    "short":      8_000,    # 短暫等待
}


class GrokVideoAutomation:
    def __init__(
        self,
        username: str,
        password: str,
        output_dir: Path,
        handle: str = "",
        login_method: str = "x",
    ):
        self.username = username
        self.password = password
        self.handle = handle      # X 使用者名稱（@後面的部分），用於異常登入驗證
        self.output_dir = output_dir
        # login_method: "x"     → 透過 x.com 登入（原流程）
        #               "email" → 直接在 accounts.x.ai 用 email 登入（不跳轉 x.com）
        self.login_method = login_method.strip().lower()
        self._playwright = None
        self._browser: Browser = None
        self._context: BrowserContext = None
        self._page: Page = None
        self._captured_video_urls: list[str] = []
        self._last_page_url: str = ""

    # ──────────────────────────────────────────────
    # 啟動 / 關閉
    # ──────────────────────────────────────────────
    async def start(self, headless: bool = False, cdp_url: str = ""):
        """
        啟動瀏覽器。優先順序：
        1. CDP 模式：連接已開啟的 Chrome（--remote-debugging-port=9222），完全繞過 Cloudflare
        2. 持久個人資料：使用 BROWSER_PROFILE_DIR 目錄保存 cf_clearance Cookie
        """
        self._playwright = await async_playwright().start()

        # ── 模式 1：連接既有 Chrome（CDP）──
        if cdp_url:
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
                contexts = self._browser.contexts
                self._context = contexts[0] if contexts else await self._browser.new_context()
                self._context.on("page", self._on_new_page)
                pages = self._context.pages
                self._page = pages[0] if pages else await self._context.new_page()
                self._page.on("response", self._on_response)
                logger.info("已連接至既有 Chrome（CDP：%s）", cdp_url)
                return
            except Exception as e:
                logger.warning("CDP 連接失敗：%s，退回持久個人資料模式", e)

        # ── 模式 2：持久個人資料（保存 cf_clearance）──
        profile_dir = Path(BROWSER_PROFILE_DIR)
        profile_dir.mkdir(parents=True, exist_ok=True)

        common_args = [
            "--disable-blink-features=AutomationControlled",
            "--window-size=1280,900",
            "--lang=zh-TW",
        ]

        launched = False
        for channel in ("chrome", None):
            try:
                kwargs = dict(
                    user_data_dir=str(profile_dir),
                    headless=headless,
                    args=common_args,
                    viewport={"width": 1280, "height": 900},
                )
                if channel:
                    kwargs["channel"] = channel
                self._context = await self._playwright.chromium.launch_persistent_context(
                    **kwargs
                )
                logger.info("使用持久個人資料啟動（%s）", channel or "Chromium")
                launched = True
                break
            except Exception as e:
                logger.debug("launch_persistent_context 失敗（%s）：%s", channel, e)

        if not launched:
            raise RuntimeError("無法啟動瀏覽器，請確認已安裝 Chrome 或 Chromium")

        self._context.on("page", self._on_new_page)
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._page.on("response", self._on_response)

    async def stop(self):
        """關閉瀏覽器與 Playwright（持久 context 會自動保存 cookies）"""
        if self._context:
            await self._context.close()
        # launch_persistent_context 沒有獨立 browser 物件
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    # ──────────────────────────────────────────────
    # 網路攔截：蒐集 MP4 URL
    # ──────────────────────────────────────────────
    async def _on_response(self, response: Response):
        url = response.url
        content_type = response.headers.get("content-type", "")
        if "video" in content_type or url.endswith(".mp4") or "mp4" in url:
            # 排除 Discover 版塊的公開分享影片（無下載權限），只收自己生成的
            if "imagine-public.x.ai" in url:
                logger.debug("略過公開分享影片（非本人生成）：%s", url[:80])
                return
            if url not in self._captured_video_urls:
                logger.debug("攔截到影片 URL：%s", url)
                self._captured_video_urls.append(url)

    def _reset_captured_urls(self):
        self._captured_video_urls.clear()

    def _on_new_page(self, page: Page):
        """Context 開啟新分頁時，只掛上 response 監聽器。
        若當前頁面已關閉才自動切換，避免背景彈窗/廣告分頁覆蓋 self._page。"""
        url = page.url or ""
        logger.info("偵測到新分頁：%s", url or "(loading)")
        page.on("response", self._on_response)
        # 僅在當前頁面已關閉時才切換
        if self._page and self._page.is_closed():
            logger.info("原分頁已關閉，切換至新分頁")
            self._page = page

    def _ensure_page(self):
        """確保 self._page 指向有效的頁面；優先選取 grok.com 分頁"""
        if self._page and not self._page.is_closed():
            return
        pages = [p for p in self._context.pages if not p.is_closed()]
        if not pages:
            return
        # 優先選 grok.com 分頁，否則取最後一個
        grok_pages = [p for p in pages if "grok.com" in (p.url or "")]
        new_page = grok_pages[-1] if grok_pages else pages[-1]
        logger.info("原分頁已關閉，切換至：%s", new_page.url or "(loading)")
        self._page = new_page
        self._page.on("response", self._on_response)

    # ──────────────────────────────────────────────
    # 登入
    # ──────────────────────────────────────────────
    async def ensure_logged_in(self) -> bool:
        """確認已登入，否則執行登入流程"""
        self._ensure_page()
        logger.info("前往 %s", GROK_IMAGINE_URL)
        await self._page.goto(GROK_IMAGINE_URL, wait_until="domcontentloaded",
                              timeout=TIMEOUTS["navigation"])
        # 等頁面 JS 渲染穩定，並等待 Cloudflare 驗證通過
        await self._page.wait_for_timeout(3000)
        await self._wait_for_cloudflare(timeout_ms=60000)

        # 先處理 Cookie 框，再關閉其他彈窗，最後判斷登入狀態
        await self._accept_cookies()
        await self._close_modal()

        if await self._is_logged_in():
            logger.info("Session 有效，已登入")
            return True

        logger.info("偵測到未登入，開始登入流程（方式：%s）…", self.login_method)
        if self.login_method == "email":
            return await self._login_via_email()
        return await self._login_via_x()

    async def _handle_unusual_activity_check(self):
        """
        處理 X 的異常登入驗證：
        若出現「輸入你的電話號碼或使用者名稱」，自動填入 handle 並繼續。
        """
        try:
            # 偵測是否出現驗證輸入框（提示文字包含「電話」或「使用者名稱」）
            verify_input = await self._page.wait_for_selector(
                'input[data-testid="ocfEnterTextTextInput"], input[name="text"][autocomplete="on"]',
                timeout=3000, state="visible"
            )
            logger.info("偵測到異常登入驗證，填入使用者名稱：%s", self.handle)
            await verify_input.fill(self.handle)
            # 點擊「下一步」
            try:
                next_btn = await self._page.wait_for_selector(
                    'button[data-testid="ocfEnterTextNextButton"], button:has-text("下一步"), button:has-text("Next")',
                    timeout=3000, state="visible"
                )
                await next_btn.click()
            except PlaywrightTimeoutError:
                await self._page.keyboard.press("Enter")
            logger.info("已通過異常登入驗證")
            await self._page.wait_for_timeout(1000)
        except PlaywrightTimeoutError:
            # 沒有出現驗證框，正常流程繼續
            pass

    async def _handle_oauth_authorize(self):
        """
        處理 X 的 OAuth 授權確認頁：
        若出現「授權應用程式」按鈕，自動點擊。
        """
        try:
            authorize_btn = await self._page.wait_for_selector(
                'button:has-text("授權應用程式"), button:has-text("Authorize app"), '
                'input[value="Authorize app"]',
                timeout=5000, state="visible"
            )
            logger.info("偵測到 OAuth 授權頁，點擊「授權應用程式」…")
            async with self._page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
                await authorize_btn.click()
            logger.info("已授權，目前頁面：%s", self._page.url)
        except PlaywrightTimeoutError:
            # 沒有出現授權頁，正常流程繼續
            pass

    async def _accept_cookies(self):
        """若出現 Cookie 同意框，自動點擊「接受所有 Cookie」"""
        combined = ", ".join(SELECTORS["cookie_accept_btns"])
        try:
            btn = await self._page.wait_for_selector(combined, timeout=3000, state="visible")
            await btn.click()
            logger.info("已接受 Cookie")
            # OneTrust 點擊後可能觸發頁面重整，等待頁面穩定
            await self._page.wait_for_load_state("domcontentloaded")
            await self._page.wait_for_timeout(1500)
        except PlaywrightTimeoutError:
            pass  # 沒有 Cookie 框，繼續

    async def _close_modal(self):
        """嘗試關閉歡迎彈窗 / 範例框（排除 cookie 相關 dialog）"""
        # 排除 OneTrust cookie 對話框，避免誤點 cookie 相關按鈕
        combined = ", ".join(
            s for s in SELECTORS["modal_close_btns"]
            if s != 'div[role="dialog"] button'
        )
        try:
            btn = await self._page.wait_for_selector(combined, timeout=2000, state="visible")
            await btn.click()
            logger.info("已關閉彈窗")
            await self._page.wait_for_timeout(800)
        except PlaywrightTimeoutError:
            pass  # 沒有彈窗，繼續

    async def _wait_for_cloudflare(self, timeout_ms: int = 60000) -> bool:
        """
        若頁面顯示 Cloudflare 安全驗證，等待其自動通過。
        判斷通過的標準：頁面上出現 grok.com 的實際互動元素（影片模式按鈕或輸入框）。
        回傳 True 表示頁面已就緒；False 表示超時。
        """
        cf_keywords = ["正在執行安全驗證", "Just a moment", "Checking your browser"]
        # 先快速確認是否有 Cloudflare 挑戰
        try:
            text = await self._page.evaluate(
                "() => document.body ? document.body.innerText.substring(0, 300) : ''"
            )
            if not any(kw in text for kw in cf_keywords):
                return True  # 沒有 CF 挑戰，直接通過
        except Exception:
            pass

        # 等待 grok.com 實際互動元素出現，表示 CF 挑戰已通過
        ready_selector = (
            f'{SELECTORS["video_mode_btn"]}, '
            f'{SELECTORS["prompt_input"]}'
        )
        # 先自動等待 30 秒
        logger.info("偵測到 Cloudflare 安全驗證，等待自動通過（30s）…")
        try:
            await self._page.wait_for_selector(ready_selector, timeout=30000, state="attached")
            logger.info("Cloudflare 驗證通過，頁面已就緒")
            return True
        except PlaywrightTimeoutError:
            pass

        # 自動等待失敗 → 提示使用者在瀏覽器視窗手動完成驗證
        logger.warning("=" * 60)
        logger.warning("Cloudflare 安全驗證需要手動操作！")
        logger.warning("請在開啟的瀏覽器視窗中完成安全驗證，")
        logger.warning("完成後程式將自動繼續（等待最多 %ds）", timeout_ms // 1000)
        logger.warning("=" * 60)
        try:
            await self._page.wait_for_selector(
                ready_selector, timeout=timeout_ms, state="attached"
            )
            logger.info("Cloudflare 驗證完成，頁面已就緒")
            return True
        except PlaywrightTimeoutError:
            logger.warning("Cloudflare 驗證等待超時（%ds）", timeout_ms // 1000)
            return False

    async def _is_logged_in(self) -> bool:
        """
        判斷是否已登入。
        先確認不是 Cloudflare 挑戰頁，再檢查 Sign In 按鈕是否存在。
        """
        # Cloudflare 挑戰頁面沒有 Sign In 按鈕，需先排除
        try:
            text = await self._page.evaluate(
                "() => document.body ? document.body.innerText.substring(0, 300) : ''"
            )
            cf_keywords = ["正在執行安全驗證", "Just a moment", "Checking your browser"]
            if any(kw in text for kw in cf_keywords):
                logger.info("頁面為 Cloudflare 驗證頁，尚未確認登入狀態")
                return False
        except Exception:
            pass

        try:
            sign_in = self._page.locator(SELECTORS["sign_in_btn"]).first
            visible = await sign_in.is_visible()
            if visible:
                logger.info("偵測到 Sign In 按鈕 → 未登入")
                return False
        except Exception:
            pass

        logger.info("未偵測到 Sign In 按鈕 → 已登入")
        return True

    async def _go_to_accounts_xai(self) -> bool:
        """
        共用步驟：從 grok.com 點擊登入按鈕，跳轉至 accounts.x.ai。
        成功回傳 True，失敗回傳 False。
        """
        sign_in = await self._page.wait_for_selector(
            SELECTORS["sign_in_btn"], timeout=TIMEOUTS["short"], state="visible"
        )
        async with self._page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
            await sign_in.click()
            logger.info("已點擊登入，等待跳轉至 accounts.x.ai…")

        await self._page.wait_for_load_state("domcontentloaded")
        logger.info("已到達：%s", self._page.url)

        if "x.ai" not in self._page.url:
            logger.error("預期跳轉至 accounts.x.ai，實際 URL：%s", self._page.url)
            return False
        await self._page.wait_for_timeout(1000)
        return True

    async def _save_session_and_verify(self) -> bool:
        """
        共用步驟：（若尚未到 grok.com）等待導回、儲存 session、確認已登入。
        """
        # OAuth 授權頁（如有）
        await self._handle_oauth_authorize()

        # 若還不在 grok.com，再等一次
        if "grok.com" not in self._page.url:
            try:
                await self._page.wait_for_url(
                    "*grok.com*", timeout=30_000, wait_until="domcontentloaded"
                )
            except PlaywrightTimeoutError:
                if "grok.com" in self._page.url:
                    logger.info("已快速跳轉至 grok.com（%s），繼續流程", self._page.url)
                else:
                    logger.error("等待 grok.com 超時，目前頁面：%s", self._page.url)
                    return False

        logger.info("已在 grok.com，等待頁面穩定…")
        await self._page.wait_for_timeout(3000)
        await self._accept_cookies()
        await self._close_modal()

        if not await self._is_logged_in():
            logger.error("登入後仍偵測到 Sign In 按鈕，請確認帳密是否正確")
            return False

        # 持久 context 的 cookies 已自動保存在 BROWSER_PROFILE_DIR
        # 額外儲存一份 session 檔案供備份 / 舊版相容
        try:
            Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
            await self._context.storage_state(path=SESSION_FILE)
            logger.info("登入成功，session 備份至 %s", SESSION_FILE)
        except Exception as e:
            logger.debug("storage_state 備份失敗（可忽略）：%s", e)
        logger.info("登入成功")
        return True

    async def _login_via_x(self) -> bool:
        """
        登入流程 A（原流程，三段跳轉）：
        grok.com → accounts.x.ai → 點「使用 X 登录」→ x.com → grok.com
        """
        try:
            # ── 第 1 段：grok.com → accounts.x.ai ──
            if not await self._go_to_accounts_xai():
                return False

            # ── 第 2 段：accounts.x.ai 點「使用 X 登录」→ x.com ──
            x_btn = await self._page.wait_for_selector(
                'button:has-text("使用 𝕏 登录"), button:has-text("Sign in with X"), button:has-text("Continue with X")',
                timeout=10_000, state="visible"
            )
            async with self._page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
                await x_btn.click()
                logger.info("已點擊「使用 X 登录」，等待跳轉至 x.com…")

            await self._page.wait_for_load_state("domcontentloaded")
            logger.info("已到達：%s", self._page.url)

            if "x.com" not in self._page.url:
                logger.error("預期跳轉至 x.com，實際 URL：%s", self._page.url)
                return False
            await self._page.wait_for_timeout(1000)

            # ── 第 3 段：x.com 輸入帳號密碼 ──
            await self._page.wait_for_selector(
                SELECTORS["x_username_input"], timeout=TIMEOUTS["login"], state="visible"
            )
            await self._page.fill(SELECTORS["x_username_input"], self.username)
            logger.info("已輸入帳號")

            try:
                next_btn = await self._page.wait_for_selector(
                    SELECTORS["x_next_btn"], timeout=5000, state="visible"
                )
                await next_btn.click()
            except PlaywrightTimeoutError:
                await self._page.keyboard.press("Enter")
            logger.info("已點擊 Next")
            await self._page.wait_for_timeout(1500)

            await self._handle_unusual_activity_check()

            await self._page.wait_for_selector(
                SELECTORS["x_password_input"], timeout=TIMEOUTS["login"], state="visible"
            )
            await self._page.fill(SELECTORS["x_password_input"], self.password)
            logger.info("已輸入密碼")

            try:
                login_btn = await self._page.wait_for_selector(
                    SELECTORS["x_login_btn"], timeout=5000, state="visible"
                )
                async with self._page.expect_navigation(wait_until="domcontentloaded", timeout=30_000):
                    await login_btn.click()
                    logger.info("已送出登入，等待導回 grok.com…")
            except PlaywrightTimeoutError:
                await self._page.keyboard.press("Enter")
                await self._page.wait_for_load_state("domcontentloaded", timeout=30_000)

            logger.info("登入後頁面 URL：%s", self._page.url)
            return await self._save_session_and_verify()

        except PlaywrightTimeoutError as e:
            logger.error("登入流程超時（方式 X）：%s", e)
            logger.error("目前頁面 URL：%s", self._page.url)
            return False
        except Exception as e:
            logger.error("登入時發生未預期錯誤（方式 X）：%s", e)
            return False

    async def _login_via_email(self) -> bool:
        """
        登入流程 B（直接 email 登入，不跳轉 x.com）：
        grok.com → accounts.x.ai → 點「使用邮箱登录」→ 填 email → 填密碼 → grok.com
        整個過程保持在 accounts.x.ai，不跳轉至 x.com。
        """
        try:
            # ── 第 1 段：grok.com → accounts.x.ai ──
            if not await self._go_to_accounts_xai():
                return False

            # ── 第 2 段：點「使用邮箱登录」按鈕 ──
            email_login_btn = await self._page.wait_for_selector(
                'button:has-text("使用邮箱登录"), button:has-text("使用電子郵件登入"), '
                'button:has-text("Sign in with email"), button:has-text("Continue with email")',
                timeout=10_000, state="visible"
            )
            await email_login_btn.click()
            logger.info("已點擊「使用邮箱登录」")
            await self._page.wait_for_timeout(2000)
            logger.info("目前頁面 URL：%s", self._page.url)

            # ── 第 3 段：填入 email（用 type() 觸發 React onChange）──
            email_input = await self._page.wait_for_selector(
                'input[data-testid="email"], input[name="email"], input[type="email"]',
                timeout=TIMEOUTS["login"], state="visible"
            )
            await email_input.click()
            await email_input.type(self.username, delay=50)
            logger.info("已輸入 email：%s", self.username)

            # 下一步
            try:
                next_btn = await self._page.wait_for_selector(
                    'button:has-text("下一步"), button:has-text("Next"), button:has-text("Continue")',
                    timeout=5000, state="visible"
                )
                await next_btn.click()
            except PlaywrightTimeoutError:
                await self._page.keyboard.press("Enter")
            logger.info("已點擊「下一步」")

            # 等待密碼頁面（含 Turnstile）完整載入
            await self._page.wait_for_timeout(5000)

            # ── 第 4 段：填入密碼（用 type() 觸發 React onChange）──
            password_input = await self._page.wait_for_selector(
                'input[type="password"], input[name="password"]',
                timeout=TIMEOUTS["login"], state="visible"
            )
            await password_input.click()
            await password_input.type(self.password, delay=50)
            logger.info("已輸入密碼")

            # 給 Turnstile 額外時間完成驗證
            await self._page.wait_for_timeout(4000)

            # 登入按鈕：使用 type="submit" + class 包含 w-full 精確定位
            try:
                login_btn = await self._page.wait_for_selector(
                    'button[type="submit"].w-full, '
                    'button[type="submit"]:has-text("登录"), '
                    'button[type="submit"]:has-text("Log in")',
                    timeout=5000, state="visible"
                )
                await login_btn.click()
                logger.info("已點擊登入按鈕（email 方式）")
            except PlaywrightTimeoutError:
                await self._page.keyboard.press("Enter")
                logger.info("找不到登入按鈕，改用 Enter 送出")

            # 等待 accounts.x.ai 自動完成認證並重定向（最多等 40 秒，每 2 秒確認一次）
            logger.info("等待 accounts.x.ai 完成認證並重定向…")
            deadline_sec = 40
            for _ in range(deadline_sec // 2):
                await self._page.wait_for_timeout(2000)
                if "grok.com" in self._page.url:
                    logger.info("已重定向至 grok.com：%s", self._page.url)
                    break
                current = self._page.url
                if "accounts.x.ai" not in current:
                    logger.info("URL 已變更至：%s", current)
                    break
            else:
                logger.info("40 秒內未自動重定向，目前 URL：%s", self._page.url)

            logger.info("目前 URL：%s", self._page.url)
            return await self._save_session_and_verify()

        except PlaywrightTimeoutError as e:
            logger.error("登入流程超時（方式 email）：%s", e)
            logger.error("目前頁面 URL：%s", self._page.url)
            return False
        except Exception as e:
            logger.error("登入時發生未預期錯誤（方式 email）：%s", e)
            return False

    # ──────────────────────────────────────────────
    # 影片生成
    # ──────────────────────────────────────────────
    async def generate_and_download(
        self, prompt: str, output_path: Path, image_path: Path | None = None
    ) -> bool:
        """
        給定 prompt（和可選圖片），生成影片並下載至 output_path。
        image_path：若提供，使用「動畫圖像」模式上傳圖片後生成影片。
        成功回傳 True，失敗回傳 False。
        """
        self._ensure_page()
        # 1. 每次生成前重新導航，確保 UI 回到初始輸入狀態
        # 若導航觸發 Cloudflare 開新分頁並關閉原頁，自動切換後重試一次
        for _nav_attempt in range(2):
            try:
                await self._page.goto(GROK_IMAGINE_URL, wait_until="domcontentloaded",
                                      timeout=TIMEOUTS["navigation"])
                await self._page.wait_for_timeout(3000)
                await self._wait_for_cloudflare(timeout_ms=30000)
                await self._accept_cookies()
                await self._close_modal()
                break
            except PlaywrightError as e:
                if "closed" in str(e).lower() and _nav_attempt == 0:
                    logger.info("頁面在導航中被關閉（Cloudflare 重導向），等待新分頁後重試…")
                    await asyncio.sleep(3)
                    self._ensure_page()
                else:
                    raise

        # 等待 prompt 輸入框出現，確認 Imagine 頁面已就緒
        try:
            await self._page.wait_for_selector(
                SELECTORS["prompt_input"], timeout=15000, state="visible"
            )
            logger.info("Imagine 頁面就緒")
        except PlaywrightTimeoutError:
            try:
                await self._page.screenshot(path="debug_page_state.png", full_page=True)
                diag = await self._page.evaluate("""
                    () => Array.from(document.querySelectorAll(
                        'input,textarea,[contenteditable],[role="textbox"],[role="radio"],button'
                    )).slice(0, 20).map(e => ({
                        tag: e.tagName, role: e.getAttribute('role')||'',
                        ce: e.getAttribute('contenteditable')||'',
                        text: (e.innerText||'').trim().substring(0,30)
                    }))
                """)
                logger.error("Imagine 頁面未就緒，已存截圖 debug_page_state.png，元素：%s  URL：%s", diag, self._page.url)
            except Exception as ex:
                logger.error("Imagine 頁面未就緒（找不到 prompt 輸入框），URL：%s  (%s)", self._page.url, ex)
            return False

        if image_path:
            # 2a. 圖片模式：附加圖片（會自動切換至影片模式）
            if not await self._attach_image(image_path):
                return False
        else:
            # 2b. 純文字模式：手動切換至影片模式
            await self._switch_to_video_mode()

        # 3. 確認影片設定（10s / 9:16）
        await self._ensure_video_settings()

        # 4. 輸入 prompt
        if not await self._fill_prompt(prompt):
            return False

        # 5. 送出前：快照頁面上已知的所有影片 URL，送出後只接受新出現的 URL
        known_srcs = await self._snapshot_video_srcs()
        self._reset_captured_urls()
        logger.info("送出前已知影片 URL 數：%d", len(known_srcs))

        await self._submit()
        logger.info("已送出 prompt，等待影片生成（最長 %.0f 秒）…",
                    TIMEOUTS["generation"] / 1000)

        # 6. 等待生成完成（排除已知舊影片）
        video_url = await self._wait_for_video(known_srcs)

        # 7. 下載影片
        # 優先使用 UI 下載按鈕（CDP 模式最可靠，繞過 Cloudflare）
        if await self._download_via_ui(output_path):
            return True
        # 若 UI 下載失敗且有 URL → 退回 httpx
        if video_url:
            logger.info("UI 下載未成功，改用 HTTP 下載…")
            return await self._download_video(video_url, output_path)
        logger.error("未能取得影片 URL，且 UI 下載按鈕未找到")
        return False

    async def _attach_image(self, image_path: Path) -> bool:
        """
        點擊「附加」→「動畫圖像」，上傳圖片至 prompt 輸入區。
        選擇「動畫圖像」會自動切換成影片模式，無需再手動切換。
        成功回傳 True，失敗回傳 False。
        """
        try:
            # 點擊「附加」按鈕展開選單
            attach_btn = await self._page.wait_for_selector(
                SELECTORS["attach_btn"], timeout=TIMEOUTS["short"], state="visible"
            )
            await attach_btn.click()
            logger.info("已點擊「附加」按鈕")
            await self._page.wait_for_timeout(500)

            # 點擊「動畫圖像」選單項，同時攔截 File Chooser
            async with self._page.expect_file_chooser(timeout=8000) as fc_info:
                animate_item = await self._page.wait_for_selector(
                    SELECTORS["animate_menuitem"], timeout=5000, state="visible"
                )
                await animate_item.click()

            file_chooser = await fc_info.value
            await file_chooser.set_files(str(image_path))
            logger.info("已上傳圖片：%s", image_path.name)

            # 等待「Remove image」按鈕出現，確認圖片縮圖已顯示、UI 已穩定
            await self._page.wait_for_selector(
                'button[aria-label="Remove image"], button:has-text("Remove image")',
                timeout=15_000, state="visible"
            )
            logger.info("圖片已顯示於輸入區，UI 就緒")
            return True

        except PlaywrightTimeoutError as e:
            logger.error("附加圖片失敗（timeout）：%s", e)
            return False
        except Exception as e:
            logger.error("附加圖片時發生錯誤：%s", e)
            return False

    async def _switch_to_video_mode(self):
        """切換至影片生成模式（點擊 radiogroup 中的「影片」radio）"""
        loc = self._page.locator(SELECTORS["video_mode_btn"])
        try:
            await loc.first.wait_for(state="visible", timeout=5000)
            aria_checked = await loc.first.get_attribute("aria-checked")
            if aria_checked == "true":
                logger.info("已在影片模式，無需切換")
                return
            await loc.first.click()
            logger.info("已切換至影片模式")
            await self._page.wait_for_timeout(800)
        except PlaywrightTimeoutError:
            logger.warning("找不到影片模式切換按鈕")

    async def _ensure_video_settings(self) -> bool:
        """
        進入影片模式後，確認並設定三項生成參數：
          - 解析度：720p
          - 時長：10 秒
          - 比例：9:16
        若設定已正確則跳過，否則點擊正確選項。
        任一項找不到對應控制項時只記錄警告，不中止流程。
        """
        logger.info("── 檢查影片設定（720p / 10s / 9:16）──")

        # 有些 UI 需先展開設定面板，嘗試一次
        await self._try_open_settings_panel()

        all_ok = True

        # ── 0. 解析度：720p（非強制，無此選項時僅警告）──
        await self._ensure_setting(
            label="解析度",
            target_text="720p",
            selector=SELECTORS["resolution_hd"],
        )

        # ── 1. 時長：10 秒 ──
        if not await self._ensure_setting(
            label="時長",
            target_text="10",
            selector=SELECTORS["duration_10s"],
        ):
            all_ok = False

        # ── 2. 比例：9:16（需先點展開按鈕，再選選單中的 9:16）──
        if not await self._ensure_aspect_ratio():
            all_ok = False

        if all_ok:
            logger.info("影片設定確認完畢：720p / 10s / 9:16 [OK]")
        else:
            logger.warning("部分影片設定控制項未找到，請手動確認頁面設定是否正確")

        return all_ok

    async def _ensure_aspect_ratio(self) -> bool:
        """
        確認並設定影片比例為 9:16。
        Grok 頁面上比例是一個顯示「目前值」的按鈕（aria-label='長寬比'）：
          - 若已顯示 9:16 → 不動作
          - 否則點擊展開選單，再選 9:16
        """
        try:
            ratio_btn = await self._page.wait_for_selector(
                SELECTORS["aspect_ratio_btn"], timeout=5000, state="visible"
            )
            current_text = (await ratio_btn.inner_text()).strip()
            logger.info("【比例】目前值：%s", current_text)

            if current_text == "9:16":
                logger.info("【比例】已是正確設定：9:16 [OK]")
                return True

            # 展開選單
            await ratio_btn.click()
            logger.info("【比例】已點擊展開選單，尋找 9:16 選項…")
            await self._page.wait_for_timeout(600)

            # 選 9:16
            option = await self._page.wait_for_selector(
                SELECTORS["aspect_9_16"], timeout=5000, state="visible"
            )
            await option.click()
            logger.info("【比例】已設定為：9:16")
            await self._page.wait_for_timeout(500)
            return True

        except Exception as e:
            logger.warning("【比例】設定失敗：%s", e)
            return False

    async def _try_open_settings_panel(self):
        """若存在設定展開按鈕，嘗試點擊以顯示設定選項"""
        try:
            btn = await self._page.wait_for_selector(
                SELECTORS["settings_btn"], timeout=2000, state="visible"
            )
            await btn.click()
            logger.info("已展開設定面板")
            await self._page.wait_for_timeout(600)
        except PlaywrightTimeoutError:
            pass  # 設定選項可能直接顯示在頁面上，無需展開

    async def _ensure_setting(self, label: str, target_text: str, selector: str) -> bool:
        """
        找到 selector 對應的所有選項，判斷目標是否已選中。
        若未選中則點擊；CSS selector 找不到時再嘗試 JS 全文搜尋。
        """
        try:
            elements = await self._page.query_selector_all(selector)

            # CSS selector 找不到時，改用 JS 掃描頁面上所有可點擊元素的文字
            if not elements:
                logger.debug("【%s】CSS selector 未命中，改用 JS 全文搜尋「%s」", label, target_text)
                elements = await self._page.evaluate_handle(
                    """(targetText) => {
                        const clickable = ['button', 'div[role="button"]', 'div[role="radio"]',
                                           'div[role="option"]', 'label', 'li', 'span'];
                        const found = [];
                        clickable.forEach(sel => {
                            document.querySelectorAll(sel).forEach(el => {
                                if (el.innerText && el.innerText.trim() === targetText) {
                                    found.push(el);
                                }
                            });
                        });
                        return found;
                    }""",
                    target_text,
                )
                # evaluate_handle 回傳 JSHandle，轉換成 element list
                elements = await elements.get_properties()
                elements = [v.as_element() for v in elements.values() if v.as_element()]

            if not elements:
                # 診斷：印出頁面上所有按鈕的文字與 aria-label，幫助定位正確 selector
                try:
                    diag = await self._page.evaluate("""
                        () => {
                            const results = [];
                            document.querySelectorAll('button, [role="radio"], [role="button"]').forEach(el => {
                                const text  = (el.innerText || '').trim().replace(/\\n/g, ' ').substring(0, 40);
                                const label = el.getAttribute('aria-label') || '';
                                const title = el.getAttribute('title') || '';
                                const val   = el.getAttribute('data-value') || '';
                                const entry = [text, label, title, val].filter(Boolean).join(' | ');
                                if (entry) results.push(entry.substring(0, 80));
                            });
                            return [...new Set(results)].slice(0, 60);
                        }
                    """)
                    logger.warning("【%s】診斷 - 按鈕文字/aria-label/title/data-value：%s", label, diag)
                except Exception:
                    pass
                logger.warning("【%s】找不到設定選項（target: %s）", label, target_text)
                return False

            for el in elements:
                try:
                    text = (await el.inner_text()).strip()
                except Exception:
                    continue

                if target_text.lower() not in text.lower():
                    continue

                # 判斷是否已選中
                aria_checked  = await el.get_attribute("aria-checked")
                aria_selected = await el.get_attribute("aria-selected")
                data_selected = await el.get_attribute("data-selected")
                class_name    = (await el.get_attribute("class")) or ""

                already_selected = (
                    aria_checked  == "true"
                    or aria_selected == "true"
                    or data_selected == "true"
                    or "selected" in class_name.lower()
                    or "active"   in class_name.lower()
                    or "checked"  in class_name.lower()
                )

                if already_selected:
                    logger.info("【%s】已是正確設定：%s [OK]", label, text)
                    return True

                # 未選中 → 點擊
                logger.info("【%s】目前設定不符，點擊選項：%s", label, text)
                await el.click()
                await self._page.wait_for_timeout(500)
                logger.info("【%s】已設定為：%s", label, text)
                return True

            logger.warning("【%s】找到選項元素但無法比對目標值「%s」", label, target_text)
            return False

        except Exception as e:
            logger.warning("【%s】設定檢查時發生例外：%s", label, e)
            return False

    async def _fill_prompt(self, prompt: str) -> bool:
        """找到輸入框並填入 prompt"""
        self._ensure_page()
        try:
            input_el = await self._page.wait_for_selector(
                SELECTORS["prompt_input"], timeout=15_000, state="visible"
            )
            await input_el.click()
            await input_el.fill(prompt)      # 一次性填入，不受長度限制
            logger.info("已填入 prompt（%d 字元）", len(prompt))
            return True
        except PlaywrightTimeoutError:
            logger.error("找不到 prompt 輸入框")
            return False

    async def _submit(self):
        """送出 prompt"""
        self._ensure_page()
        try:
            btn = await self._page.wait_for_selector(
                SELECTORS["submit_btn"], timeout=5000, state="visible"
            )
            await btn.click()
        except PlaywrightTimeoutError:
            logger.info("找不到送出按鈕，改用 Enter 鍵送出")
            await self._page.keyboard.press("Enter")

    async def _snapshot_video_srcs(self) -> set[str]:
        """取得頁面上目前所有影片 URL（送出前快照，用於後續過濾）"""
        srcs: set[str] = set()
        try:
            result = await self._page.evaluate("""
                () => {
                    const urls = [];
                    document.querySelectorAll('video[src]').forEach(v => {
                        if (v.src && v.src.startsWith('http')) urls.push(v.src);
                    });
                    document.querySelectorAll('video source[src]').forEach(s => {
                        if (s.src && s.src.startsWith('http')) urls.push(s.src);
                    });
                    return urls;
                }
            """)
            srcs.update(result)
        except Exception:
            pass
        # 也把目前已攔截的 URL 加入快照（包含尚未渲染至 DOM 的歷史影片）
        srcs.update(self._captured_video_urls)
        return srcs

    async def _wait_for_video(self, known_srcs: set[str] | None = None) -> str | None:
        """
        等待影片生成完成，回傳影片 URL。
        known_srcs：送出前快照的舊 URL 集合，用於過濾歷史影片。
        策略（依序嘗試）：
          1. 監看已攔截的 MP4 URL（透過 _on_response）
          2. 偵測 <video> element 的 src 屬性
          3. 偵測下載按鈕的 href
        """
        if known_srcs is None:
            known_srcs = set()

        deadline = asyncio.get_event_loop().time() + TIMEOUTS["generation"] / 1000
        poll_interval = 5  # 每 5 秒輪詢一次

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)

            # 策略 1：網路攔截到的 MP4（排除快照中的舊 URL）
            new_urls = [u for u in self._captured_video_urls if u not in known_srcs]
            if new_urls:
                url = new_urls[-1]
                logger.info("策略 1 成功：攔截到新影片 URL：%s", url[:80])
                return url

            # 策略 2：頁面中的 <video> 元素（排除已知舊 URL）
            video_src = await self._extract_video_src(known_srcs)
            if video_src:
                logger.info("策略 2 成功：video element src：%s", video_src[:80])
                return video_src

            # 策略 3：下載按鈕的 href
            download_href = await self._extract_download_href(known_srcs)
            if download_href:
                logger.info("策略 3 成功：download href：%s", download_href[:80])
                return download_href

            logger.info("仍在生成中，繼續等待… (已等 %.0fs)",
                        TIMEOUTS["generation"] / 1000 - (deadline - asyncio.get_event_loop().time()))

        logger.warning("等待影片生成超時（%.0f 秒）", TIMEOUTS["generation"] / 1000)
        return None

    async def _extract_video_src(self, known_srcs: set[str] | None = None) -> str | None:
        """從 <video> 或 <source> 元素取得 src，排除 known_srcs 中的舊 URL"""
        if known_srcs is None:
            known_srcs = set()
        try:
            srcs = await self._page.evaluate("""
                () => {
                    const urls = [];
                    document.querySelectorAll('video[src]').forEach(v => {
                        if (v.src && v.src.startsWith('http')) urls.push(v.src);
                    });
                    document.querySelectorAll('video source[src]').forEach(s => {
                        if (s.src && s.src.startsWith('http')) urls.push(s.src);
                    });
                    return urls;
                }
            """)
            for src in srcs:
                if src not in known_srcs:
                    return src
        except Exception:
            pass
        return None

    async def _extract_download_href(self, known_srcs: set[str] | None = None) -> str | None:
        """從下載按鈕取得 href"""
        if known_srcs is None:
            known_srcs = set()
        try:
            href = await self._page.eval_on_selector(
                SELECTORS["download_btn"],
                "el => el.href || el.getAttribute('href')",
                timeout=1000,
            )
            if href and href.startswith("http") and href not in known_srcs:
                return href
        except Exception:
            pass
        return None

    # ──────────────────────────────────────────────
    # 下載
    # ──────────────────────────────────────────────
    async def _download_via_ui(self, output_path: Path) -> bool:
        """
        點擊頁面上的下載按鈕，透過 Playwright expect_download 攔截檔案。
        CDP 模式下最可靠，繼承瀏覽器的 cookies 與 TLS 指紋，可繞過 Cloudflare。
        """
        try:
            dl_btn = await self._page.wait_for_selector(
                SELECTORS["download_btn"], timeout=10000, state="visible"
            )
            logger.info("找到下載按鈕，使用 UI 下載…")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            async with self._page.expect_download(timeout=TIMEOUTS["download"]) as dl_info:
                await dl_btn.click()
            dl = await dl_info.value
            await dl.save_as(str(output_path))
            size_mb = output_path.stat().st_size / 1024 / 1024
            logger.info("UI 下載完成：%s（%.1f MB）", output_path, size_mb)
            return True
        except PlaywrightTimeoutError:
            logger.warning("找不到下載按鈕或下載事件超時，改用 HTTP 下載")
            return False
        except Exception as e:
            logger.warning("UI 下載失敗：%s", e)
            return False

    async def _download_video(self, url: str, output_path: Path) -> bool:
        """下載影片至 output_path"""
        logger.info("開始下載影片：%s", url[:80])
        try:
            # 取得目前頁面的 cookies 供下載請求使用
            cookies = await self._context.cookies()
            cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(TIMEOUTS["download"] / 1000),
                follow_redirects=True,
            ) as client:
                async with client.stream(
                    "GET",
                    url,
                    headers={
                        "Cookie": cookie_header,
                        "Referer": GROK_IMAGINE_URL,
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                    },
                ) as resp:
                    if resp.status_code != 200:
                        logger.error("下載失敗，HTTP %d：%s", resp.status_code, url)
                        return False

                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(output_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=1024 * 64):
                            f.write(chunk)

            size_mb = output_path.stat().st_size / 1024 / 1024
            logger.info("下載完成：%s（%.1f MB）", output_path, size_mb)
            return True

        except Exception as e:
            logger.error("下載時發生錯誤：%s", e)
            return False
