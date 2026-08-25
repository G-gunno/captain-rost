import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from loguru import logger
from telegram.ext import Application, CommandHandler


# --- Мини веб-сервер "пульс" для Render (чтобы бесплатный тариф не засыпал) ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args, **kwargs):
        pass  # не спамим в логи


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health-сервер запущен на порту {port}")


# --- Обработчики команд Telegram ---
async def cmd_start(update, context):
    await update.message.reply_text("🤖 Капитан Рост на связи! Бот успешно запущен на сервере Render.")

async def cmd_status(update, context):
    await update.message.reply_text("📊 Статус: Тестируем подключение. Балансы пока не подключены.")


# --- Главная функция ---
def main():
    logger.info("Запуск бота CaptainRost...")

    # Запускаем "пульс" для Render
    start_health_server()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Ошибка! TELEGRAM_BOT_TOKEN не найден в настройках сервера.")
        return

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))

    logger.info("Бот успешно стартовал и слушает Telegram...")
    app.run_polling()


if __name__ == '__main__':
    main()
