"""
main.py
入口程式：掃描 ./prompts/ 中的 .txt 檔，逐一送至 grok.com/imagine 生成影片，
下載至 ./output/，完成後將 prompt 檔移至 ./prompts/done/
"""
import asyncio
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from grok_automation import GrokVideoAutomation

# ──────────────────────────────────────────────
# 路徑設定
# ──────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
PROMPTS_DIR = BASE_DIR / "prompts"
DONE_DIR    = PROMPTS_DIR / "done"
OUTPUT_DIR  = BASE_DIR / "output"


# ──────────────────────────────────────────────
# 日誌設定
# ──────────────────────────────────────────────
def setup_logging():
    log_format = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(BASE_DIR / "run.log", encoding="utf-8"),
        ],
    )


# ──────────────────────────────────────────────
# Prompt 掃描
# ──────────────────────────────────────────────
def collect_prompts() -> list[Path]:
    """回傳 prompts/ 下所有 .txt 檔（排除 done/ 子目錄），依名稱排序"""
    files = sorted(PROMPTS_DIR.glob("*.txt"))
    if not files:
        logging.warning("在 %s 中找不到任何 .txt 檔案", PROMPTS_DIR)
    return files


def read_prompt(path: Path) -> str:
    """讀取 prompt 檔案內容並去除前後空白"""
    return path.read_text(encoding="utf-8").strip()


def move_to_done(path: Path):
    """將已處理的 prompt 檔案移至 done/ 資料夾"""
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    dest = DONE_DIR / path.name
    # 若 done/ 內已有同名檔，加上時間戳避免覆蓋
    if dest.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = DONE_DIR / f"{path.stem}_{ts}{path.suffix}"
    shutil.move(str(path), str(dest))
    logging.info("已移至 done：%s", dest)


# ──────────────────────────────────────────────
# 輸出檔名
# ──────────────────────────────────────────────
def build_output_path(prompt_file: Path) -> Path:
    """
    output 檔名 = prompt 檔名（無副檔名）+ 時間戳 + .mp4
    例：my_scene_20240101_153045.mp4
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"{prompt_file.stem}_{ts}.mp4"


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
async def run():
    setup_logging()
    logger = logging.getLogger(__name__)

    # 載入 .env
    load_dotenv()
    username = os.getenv("X_USERNAME", "").strip()
    password = os.getenv("X_PASSWORD", "").strip()
    handle   = os.getenv("X_HANDLE", "").strip()

    if not username or not password:
        logger.error("請在 .env 中設定 X_USERNAME 與 X_PASSWORD")
        sys.exit(1)

    # 建立必要資料夾
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 掃描 prompt 檔案
    prompt_files = collect_prompts()
    if not prompt_files:
        logger.info("沒有待處理的 prompt，程式結束")
        return

    logger.info("共找到 %d 個 prompt 檔案，開始處理…", len(prompt_files))

    # 啟動自動化
    bot = GrokVideoAutomation(
        username=username,
        password=password,
        output_dir=OUTPUT_DIR,
        handle=handle,
    )
    await bot.start(headless=False)   # headless=True 可隱藏視窗，建議先用 False 確認流程

    error_occurred = False
    try:
        # 登入（只需一次）
        if not await bot.ensure_logged_in():
            logger.error("登入失敗，程式中止")
            error_occurred = True
            return

        # 逐一處理
        success_count = 0
        fail_count    = 0

        for idx, prompt_file in enumerate(prompt_files, 1):
            prompt_text = read_prompt(prompt_file)
            if not prompt_text:
                logger.warning("[%d/%d] %s 內容為空，跳過",
                               idx, len(prompt_files), prompt_file.name)
                move_to_done(prompt_file)
                continue

            logger.info("=" * 60)
            logger.info("[%d/%d] 處理：%s", idx, len(prompt_files), prompt_file.name)
            logger.info("Prompt：%s", prompt_text[:120])

            output_path = build_output_path(prompt_file)

            ok = await bot.generate_and_download(
                prompt=prompt_text,
                output_path=output_path,
            )

            if ok:
                logger.info("成功：影片已儲存至 %s", output_path)
                move_to_done(prompt_file)
                success_count += 1
            else:
                logger.error("失敗：%s 未能生成影片，檔案保留在 prompts/", prompt_file.name)
                fail_count += 1

            # 每個任務之間稍作間隔，避免請求過於頻繁
            if idx < len(prompt_files):
                logger.info("等待 5 秒後處理下一個…")
                await asyncio.sleep(5)

    except Exception as e:
        logger.error("發生未預期錯誤：%s", e, exc_info=True)
        error_occurred = True

    finally:
        if error_occurred:
            # 發生錯誤時保持瀏覽器開啟，讓使用者能夠手動檢查
            logger.warning("=" * 60)
            logger.warning("發生錯誤，瀏覽器保持開啟以供檢查。")
            logger.warning("確認完畢後，請在此按 Enter 關閉瀏覽器…")
            await asyncio.get_event_loop().run_in_executor(None, input)
        await bot.stop()

    # 摘要
    logger.info("=" * 60)
    logger.info("全部完成：成功 %d 支 / 失敗 %d 支", success_count, fail_count)
    if fail_count > 0:
        logger.info("失敗的 prompt 檔案仍留在 ./prompts/，修正後可重新執行")


if __name__ == "__main__":
    asyncio.run(run())
