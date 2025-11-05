# web_main.py
import os, threading, asyncio, logging
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters
from main import (
    db_init, db_purge_expired,
    handle_input_text, start, help_command, balance, buy, confirm, list_cmd
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("web_main")

app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/")
def root():
    return "healthy", 200

async def _startup():
    db_init()
    db_purge_expired()

def run_bot():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_TOKEN")

    # 1) Khởi tạo DB trong 1 event loop đang chạy
    asyncio.run(_startup())

    # 2) Tạo app Telegram
    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(CommandHandler("confirm", confirm))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input_text))

    logger.info("Starting Telegram polling (will delete webhook if set)…")
    # 3) QUAN TRỌNG: xoá webhook & xoá update cũ rồi mới polling
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,  # xoá queue cũ & deleteWebhook phía Telegram
        poll_interval=2.0
    )

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True, name="run_bot").start()
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
