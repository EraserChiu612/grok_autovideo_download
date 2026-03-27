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
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 設定區：若 grok.com UI 更新，只需修改此區塊
# ──────────────────────────────────────────────
GROK_IMAGINE_URL = "https://grok.com/imagine"
SESSION_FILE = "session/browser_context.json"

SELECTORS = {
    # ── 登入相關 ──
    # grok.com 繁中介面：登入為 <a> 連結
    "sign_in_btn":      'a[href*="sign-in"], a:has-text("登入"), button:has-text("登入"), a:has-text("Sign in")',

    # X 登入表單（在 x.com 上，介面為英文）
    "x_username_input": 'input[name="text"], input[autocomplete="username"]',
    "x_next_btn":       'button:has-text("Next"), div[role="button"]:has-text("Next")',
    "x_password_input": 'input[name="password"], input[type="password"]',
    "x_login_btn":      'button:has-text("Log in"), div[role="button"]:has-text("Log in")',

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
    # 影片模式：radiogroup 內的 radio "影片"
    "video_mode_btn":   'input[type="radio"] + * :has-text("影片"), [role="radio"]:has-text("影片")',
    # prompt 輸入框：contenteditable 的 div（段落 p 是 placeholder）
    "prompt_input":     '[contenteditable="true"], [contenteditable=""], textarea',
    # 送出按鈕：實際文字是「送出」
    "submit_btn":       'button:has-text("送出"), button[aria-label="送出"], button[aria-label="Send"], button[type="submit"]',

    # ── 圖片附加 ──
    "attach_btn":       'button:has-text("附加"), button[aria-label="附加"]',
    "animate_menuitem": '[role="menuitem"]:has-text("動畫圖像"), menuitem:has-text("動畫圖像")',

    # ── 生成完成後 ──
    "video_element":    'video[src], video source[src]',
    "download_btn":     'a[download], button:has-text("下載"), button:has-text("Download"), a:has-text("下載"), a:has-text("Download")',
    "loading_indicator":'[aria-label*="loading" i], [data-testid*="loading"], .loading, [class*="spinner"]',
}

TIMEOUTS = {
    "login":      30_000,   # 30 秒
    "navigation": 20_000,   # 20 秒
    "generation": 360_000,  # 6 分鐘（影片生成最長等待）
    "download":   60_000,   # 1 分鐘
    "short":      8_000,    # 短暫等待
}


class GrokVideoAutomation:
    def __init__(self, username: str, password: str, output_dir: Path, handle: str = ""):
        self.username = username
        self.password = password
        self.handle = handle      # X 使用者名稱（@後面的部分），用於異常登入驗證
        self.output_dir = output_dir
        self._playwright = None
        self._browser: Browser = None
        self._context: BrowserContext = None
        self._page: Page = None
        self._captured_video_urls: list[str] = []

    # ──────────────────────────────────────────────
    # 啟動 / 關閉
    # ──────────────────────────────────────────────
    async def start(self, headless: bool = False):
        """啟動瀏覽器，載入既有 session（若存在）"""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

        session_path = Path(SESSION_FILE)
        if session_path.exists():
            logger.info("載入既有 session：%s", SESSION_FILE)
            self._context = await self._browser.new_context(
                storage_state=SESSION_FILE,
                viewport={"width": 1280, "height": 900},
            )
        else:
            logger.info("無既有 session，將執行全新登入")
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 900},
            )

        self._page = await self._context.new_page()
        # 攔截所有回應，蒐集可能的影片 URL
        self._page.on("response", self._on_response)

    async def stop(self):
        """關閉瀏覽器與 Playwright"""
        if self._context:
            await self._context.close()
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

    # ──────────────────────────────────────────────
    # 登入
    # ──────────────────────────────────────────────
    async def ensure_logged_in(self) -> bool:
        """確認已登入，否則執行登入流程"""
        logger.info("前往 %s", GROK_IMAGINE_URL)
        await self._page.goto(GROK_IMAGINE_URL, wait_until="domcontentloaded",
                              timeout=TIMEOUTS["navigation"])
        # 等頁面 JS 渲染穩定
        await self._page.wait_for_timeout(3000)

        # 先關閉可能出現的範例框 / 歡迎彈窗，再判斷登入狀態
        await self._close_modal()

        if await self._is_logged_in():
            logger.info("Session 有效，已登入")
            return True

        logger.info("偵測到未登入，開始登入流程…")
        return await self._login()

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

    async def _close_modal(self):
        """嘗試關閉歡迎彈窗 / 範例框"""
        for sel in SELECTORS["modal_close_btns"]:
            try:
                btn = await self._page.wait_for_selector(sel, timeout=2000, state="visible")
                await btn.click()
                logger.info("已關閉彈窗（selector: %s）", sel)
                await self._page.wait_for_timeout(800)
                return  # 關一個就夠
            except PlaywrightTimeoutError:
                continue

    async def _is_logged_in(self) -> bool:
        """
        判斷是否已登入。
        核心邏輯：Sign In 按鈕若可見 → 未登入；若找不到 Sign In 按鈕 → 已登入。
        """
        try:
            sign_in = self._page.locator(SELECTORS["sign_in_btn"]).first
            # 給 3 秒讓頁面渲染，如果 Sign In 可見就是未登入
            visible = await sign_in.is_visible()
            if visible:
                logger.info("偵測到 Sign In 按鈕 → 未登入")
                return False
        except Exception:
            pass

        # Sign In 按鈕不存在 → 已登入
        logger.info("未偵測到 Sign In 按鈕 → 已登入")
        return True

    async def _login(self) -> bool:
        """
        執行登入流程（三段跳轉）：
        grok.com → accounts.x.ai → x.com → grok.com
        注意：navigation 監聽必須在 click 之前設定，否則會錯過事件。
        """
        try:
            # ── 第 1 段：grok.com 點登入 → accounts.x.ai ──
            sign_in = await self._page.wait_for_selector(
                SELECTORS["sign_in_btn"], timeout=TIMEOUTS["short"], state="visible"
            )
            # 在點擊前設定 navigation 等待，避免錯過事件
            async with self._page.expect_navigation(wait_until="domcontentloaded", timeout=15_000):
                await sign_in.click()
                logger.info("已點擊登入，等待跳轉至 accounts.x.ai…")

            await self._page.wait_for_load_state("domcontentloaded")
            logger.info("已到達：%s", self._page.url)

            if "x.ai" not in self._page.url:
                logger.error("預期跳轉至 accounts.x.ai，實際 URL：%s", self._page.url)
                return False
            await self._page.wait_for_timeout(1000)

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

            # Next 按鈕
            try:
                next_btn = await self._page.wait_for_selector(
                    SELECTORS["x_next_btn"], timeout=5000, state="visible"
                )
                await next_btn.click()
            except PlaywrightTimeoutError:
                await self._page.keyboard.press("Enter")
            logger.info("已點擊 Next")
            await self._page.wait_for_timeout(1500)

            # ── 異常登入驗證：X 可能要求輸入電話或使用者名稱 ──
            await self._handle_unusual_activity_check()

            # 輸入密碼
            await self._page.wait_for_selector(
                SELECTORS["x_password_input"], timeout=TIMEOUTS["login"], state="visible"
            )
            await self._page.fill(SELECTORS["x_password_input"], self.password)
            logger.info("已輸入密碼")

            # Log in 按鈕
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

            # ── OAuth 授權頁：點擊「授權應用程式」──
            await self._handle_oauth_authorize()

            # 若還在 x.com / accounts.x.ai（可能有授權確認頁），再等一次導回
            if "grok.com" not in self._page.url:
                try:
                    await self._page.wait_for_url(
                        "*grok.com*", timeout=30_000, wait_until="domcontentloaded"
                    )
                except PlaywrightTimeoutError:
                    # 授權後可能快速跳轉，事件在 wait_for_url 設定前已觸發
                    # 只要當前 URL 已在 grok.com 即可繼續
                    if "grok.com" in self._page.url:
                        logger.info("已快速跳轉至 grok.com（%s），繼續流程", self._page.url)
                    else:
                        logger.error("等待 grok.com 超時，目前頁面：%s", self._page.url)
                        return False

            logger.info("已導回 grok.com，等待頁面穩定…")
            await self._page.wait_for_timeout(3000)

            # 關閉可能再次出現的歡迎彈窗
            await self._close_modal()

            if not await self._is_logged_in():
                logger.error("登入後仍偵測到 Sign In 按鈕，請確認帳密是否正確")
                return False

            # 儲存 session
            Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
            await self._context.storage_state(path=SESSION_FILE)
            logger.info("登入成功，session 已儲存至 %s", SESSION_FILE)
            return True

        except PlaywrightTimeoutError as e:
            logger.error("登入流程超時：%s", e)
            logger.error("目前頁面 URL：%s", self._page.url)
            return False
        except Exception as e:
            logger.error("登入時發生未預期錯誤：%s", e)
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
        # 1. 確認在 imagine 頁面
        await self._page.goto(GROK_IMAGINE_URL, wait_until="domcontentloaded",
                              timeout=TIMEOUTS["navigation"])
        await self._page.wait_for_timeout(3000)  # 等頁面及歷史影片請求穩定
        await self._close_modal()   # 關閉可能再次出現的彈窗

        if image_path:
            # 2a. 圖片模式：附加圖片（會自動切換至影片模式）
            if not await self._attach_image(image_path):
                return False
        else:
            # 2b. 純文字模式：手動切換至影片模式
            await self._switch_to_video_mode()

        # 3. 輸入 prompt
        if not await self._fill_prompt(prompt):
            return False

        # 4. 送出前：快照頁面上已知的所有影片 URL，送出後只接受新出現的 URL
        known_srcs = await self._snapshot_video_srcs()
        self._reset_captured_urls()
        logger.info("送出前已知影片 URL 數：%d", len(known_srcs))

        await self._submit()
        logger.info("已送出 prompt，等待影片生成（最長 %.0f 秒）…",
                    TIMEOUTS["generation"] / 1000)

        # 5. 等待生成完成（排除已知舊影片）
        video_url = await self._wait_for_video(known_srcs)
        if not video_url:
            logger.error("未能取得影片 URL")
            return False

        # 6. 下載影片
        return await self._download_video(video_url, output_path)

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

            # 等待圖片縮圖出現在輸入區（表示上傳完成）
            await self._page.wait_for_timeout(1500)
            return True

        except PlaywrightTimeoutError as e:
            logger.error("附加圖片失敗（timeout）：%s", e)
            return False
        except Exception as e:
            logger.error("附加圖片時發生錯誤：%s", e)
            return False

    async def _switch_to_video_mode(self):
        """切換至影片生成模式（點擊 radiogroup 中的「影片」radio）"""
        try:
            # 直接用 get_by_role 找 radio "影片" 最可靠
            video_radio = self._page.get_by_role("radio", name="影片")
            if await video_radio.is_visible():
                is_checked = await video_radio.is_checked()
                if not is_checked:
                    await video_radio.click()
                    logger.info("已切換至影片模式")
                    await self._page.wait_for_timeout(800)
                else:
                    logger.info("已在影片模式，無需切換")
                return
        except Exception:
            pass
        # Fallback：selector 方式
        try:
            video_btn = await self._page.wait_for_selector(
                SELECTORS["video_mode_btn"], timeout=5000
            )
            await video_btn.click()
            logger.info("已切換至影片模式（fallback selector）")
            await self._page.wait_for_timeout(800)
        except PlaywrightTimeoutError:
            logger.warning("找不到影片模式切換按鈕")

    async def _fill_prompt(self, prompt: str) -> bool:
        """找到輸入框並填入 prompt"""
        try:
            input_el = await self._page.wait_for_selector(
                SELECTORS["prompt_input"], timeout=TIMEOUTS["short"], state="visible"
            )
            await input_el.click()
            await input_el.fill("")          # 清空舊內容
            await input_el.type(prompt, delay=30)  # 逐字輸入，模擬人工操作
            logger.info("已填入 prompt（%d 字元）", len(prompt))
            return True
        except PlaywrightTimeoutError:
            logger.error("找不到 prompt 輸入框")
            return False

    async def _submit(self):
        """送出 prompt"""
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
