import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from loguru import logger
from telegram.ext import Application, CommandHandler

from bot.exchange.bybit_client import BybitClient


# --- Мини веб-сервер "пульс" для Render ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args, **kwargs):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health-сервер запущен на порту {port}")


bybit = BybitClient()

logger.info(
    f"Bybit fingerprint: key={bybit.api_key[:4]}...{bybit.api_key[-4:]} "
    f"len={len(bybit.api_key)} | secret_len={len(bybit.api_secret)} | {bybit.base_url}"
)


async def cmd_start(update, context):
    await update.message.reply_text("🤖 Капитан Рост на связи! Бот успешно запущен на сервере Render.")


async def cmd_status(update, context):
    try:
        msg = ["🔎 ПРОВЕРКА КЛЮЧА НА СЕРВЕРАХ BYBIT", ""]
        for base in [
            "https://api-testnet.bybit.com",
            "https://api-demo.bybit.com",
            "https://api.bybit.com",
        ]:
            res = await bybit.check_key_on(base)
            msg.append(f"{base.replace('https://', '')}:\n   {res}")
        msg.append("")
        msg.append(f"🔑 {bybit.api_key[:4]}...{bybit.api_key[-4:]} len={len(bybit.api_key)}")
        await update.message.reply_text("\n".join(msg))
    except Exception as e:
        logger.exception("Ошибка в /status")
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


def main():
    logger.info("Запуск бота CaptainRost...")
    start_health_server()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Ошибка! TELEGRAM_BOT_TOKEN не найден.")
        return

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))

    logger.info("Бот успешно стартовал и слушает Telegram...")
    app.run_polling()


if __name__ == '__main__':
    main()
