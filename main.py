import os
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


def format_wallet(wallet, name):
    lines = [f"💼 {name}:"]
    if not wallet:
        lines.append("   нет данных")
        return lines
    shown = False
    for c in wallet.get("coin", []):
        equity = float(c.get("equity") or c.get("walletBalance") or 0)
        if equity > 0.000001:
            lines.append(f"   {c['coin']}: {equity:.4f}")
            shown = True
    if not shown:
        lines.append("   пусто")
    return lines


async def cmd_start(update, context):
    await update.message.reply_text("🤖 Капитан Рост на связи! Бот успешно запущен на сервере Render.")


async def cmd_status(update, context):
    try:
        unified = await bybit.get_wallet_balance("UNIFIED")
        funding = await bybit.get_wallet_balance("FUND")

        msg = ["📊 СТАТУС БИРЖИ (TESTNET)", ""]
        if unified:
            total = unified.get("totalEquityValue", "0")
            msg.append(f"💰 Total Equity: {float(total):.2f} $")
            msg.append("")
        msg += format_wallet(unified, "Unified trading")
        msg.append("")
        msg += format_wallet(funding, "Funding")

        await update.message.reply_text("\n".join(msg))
    except Exception as e:
        logger.exception("Ошибка в /status")
        await update.message.reply_text(f"⚠️ Ошибка при запросе к бирже: {e}")


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
