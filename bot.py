import os
import re
import asyncio
import logging
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DOWNLOAD_PATH = "downloads"
MAX_SIZE_MB = 50  # Telegram bot limit

os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# ── Helpers ───────────────────────────────────────────────────────────────────
PLATFORM_MAP = {
    ("youtube.com", "youtu.be"): "YouTube 🎥",
    ("tiktok.com",): "TikTok 🎵",
    ("instagram.com",): "Instagram 📸",
    ("twitter.com", "x.com"): "X (Twitter) 🐦",
    ("snapchat.com",): "Snapchat 👻",
}

def get_platform(url: str) -> str | None:
    for domains, name in PLATFORM_MAP.items():
        if any(d in url for d in domains):
            return name
    return None

def extract_url(text: str) -> str | None:
    matches = re.findall(r"https?://\S+", text)
    return matches[0] if matches else None

def human_size(path: str) -> str:
    mb = os.path.getsize(path) / 1_048_576
    return f"{mb:.1f} MB"

# ── yt-dlp download (runs in executor) ───────────────────────────────────────
def _download_sync(url: str, user_id: int) -> dict:
    """Blocking download – call via run_in_executor."""
    template = f"{DOWNLOAD_PATH}/{user_id}_%(id)s.%(ext)s"

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": template,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # Instagram & TikTok sometimes need cookies / user-agent
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", "Video")
        filepath = ydl.prepare_filename(info)

        # After merge the extension is always .mp4
        if not os.path.exists(filepath):
            base = os.path.splitext(filepath)[0]
            filepath = base + ".mp4"

        # Last-resort: pick newest file for this user
        if not os.path.exists(filepath):
            candidates = sorted(
                Path(DOWNLOAD_PATH).glob(f"{user_id}_*"),
                key=os.path.getctime,
            )
            if not candidates:
                raise FileNotFoundError("Downloaded file not found.")
            filepath = str(candidates[-1])

        return {"title": title, "filepath": filepath}

def _cleanup(user_id: int):
    for f in Path(DOWNLOAD_PATH).glob(f"{user_id}_*"):
        try:
            f.unlink()
        except Exception:
            pass

# ── Handlers ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Halo! Saya Video Downloader Bot!*\n\n"
        "📥 *Platform yang didukung:*\n"
        "• 🎥 YouTube\n"
        "• 🎵 TikTok\n"
        "• 📸 Instagram\n"
        "• 🐦 X (Twitter)\n"
        "• 👻 Snapchat\n\n"
        "💡 *Cara pakai:* Cukup kirim link video ke sini!\n\n"
        "⚠️ Batas ukuran file: 50 MB (limit Telegram)",
        parse_mode="Markdown",
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Panduan Penggunaan*\n\n"
        "1️⃣ Buka aplikasi (YouTube, TikTok, dll)\n"
        "2️⃣ Copy link / URL video\n"
        "3️⃣ Paste & kirim ke bot ini\n"
        "4️⃣ Tunggu beberapa detik\n"
        "5️⃣ Video dikirim otomatis! ✅\n\n"
        "❓ *Masalah umum:*\n"
        "• Instagram private → tidak bisa didownload\n"
        "• Video >50MB → coba video yang lebih pendek\n"
        "• Error lain → coba lagi atau ganti link",
        parse_mode="Markdown",
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    url = extract_url(text)

    if not url:
        await update.message.reply_text(
            "❌ Tidak ada URL yang ditemukan.\nKirim link video yang valid ya!"
        )
        return

    platform = get_platform(url)
    if not platform:
        await update.message.reply_text(
            "❌ *Platform tidak didukung.*\n\n"
            "Platform yang bisa: YouTube, TikTok, Instagram, X, Snapchat",
            parse_mode="Markdown",
        )
        return

    user_id = update.effective_user.id
    status = await update.message.reply_text(
        f"⏳ Mendownload dari *{platform}*...\nMohon tunggu sebentar.",
        parse_mode="Markdown",
    )

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, _download_sync, url, user_id
        )

        filepath = result["filepath"]
        title = result["title"]
        size_str = human_size(filepath)
        size_mb = os.path.getsize(filepath) / 1_048_576

        if size_mb > MAX_SIZE_MB:
            await status.edit_text(
                f"❌ Video terlalu besar ({size_str}).\n"
                "Telegram hanya mendukung maksimal 50 MB.\n"
                "Coba video yang lebih pendek."
            )
            _cleanup(user_id)
            return

        await status.edit_text(f"📤 Mengirim video ({size_str})...")

        caption = (
            f"✅ *{title[:200]}*\n"
            f"📌 Platform: {platform}\n"
            f"📦 Ukuran: {size_str}"
        )

        with open(filepath, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=caption,
                parse_mode="Markdown",
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
            )

        await status.delete()

    except Exception as exc:
        logger.error("Download error for %s: %s", url, exc)
        await status.edit_text(
            f"❌ *Gagal mendownload video.*\n\n"
            f"```\n{str(exc)[:300]}\n```\n\n"
            "Pastikan link benar dan video bukan konten private.",
            parse_mode="Markdown",
        )
    finally:
        _cleanup(user_id)

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set!")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
