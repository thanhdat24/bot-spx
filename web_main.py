# web_main.py
import os, threading
from flask import Flask
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# import lại các hàm bạn đã có trong main.py
from main import (
    db_init, db_purge_expired,
    handle_input_text, start, help_command, balance, buy, confirm, list_cmd
)

app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return "ok", 200

def run_bot():
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN")

    db_init(); db_purge_expired()

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(CommandHandler("confirm", confirm))
    application.add_handler(CommandHandler("list", list_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input_text))

    # polling như cũ ⇒ logic lưu SQLite không đổi
    application.run_polling(allowed_updates=None)

if __name__ == "__main__":
    # chạy bot ở thread riêng
    threading.Thread(target=run_bot, daemon=True).start()
    # server http cho Render
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
