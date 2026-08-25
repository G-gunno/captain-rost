import os
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from loguru import logger
from telegram.ext import Application, CommandHandler

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper


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


async def cmd_start(update, context):
    await update.message.reply_text("🤖 Капитан Рост на связи! Режим: ТРЕНИРОВКА 🎓 (реальные цены, виртуальные деньги).")


async def cmd_status(update, context):
    try:
        prices = await market_data.get_tickers()
        eq = paper.equity(prices)

        msg = ["📊 СТАТУС (РЕЖИМ: ТРЕНИРОВКА 🎓)", ""]
        msg.append(f"💰 Свободно: {paper.usdt:.2f} USDT")
        msg.append(f"📈 Total Equity: {eq:.2f} $")
        msg.append(f"📦 Позиций: {len(paper.positions)} | Активных ордеров: {len(paper.orders)}")
        msg.append(f"🧾 Сделок всего: {len(paper.trades)}")
        msg.append("")

        btc = prices.get("BTCUSDT", {}).get("last", 0)
        eth = prices.get("ETHUSDT", {}).get("last", 0)
        msg.append(f"₿ BTC: {btc:,.0f} $")
        msg.append(f"Ξ ETH: {eth:,.0f} $")
        msg.append(f"🌍 Пар к USDT на рынке: {len(prices)}")

        await update.message.reply_text("\n".join(msg))
    except Exception as e:
        logger.exception("Ошибка в /status")
        await update.message.reply_text(f"⚠️ Ошибка: {e}")


def main():
    logger.info("Запуск бота CaptainRost (PAPER MODE)...")
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
