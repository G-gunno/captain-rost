import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from loguru import logger
from telegram.ext import Application, CommandHandler

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.core.orchestrator import run_cycle, set_notifier, CYCLE_SECONDS


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


async def cycle_loop():
    await asyncio.sleep(15)   # даём серверу стартануть
    while True:
        try:
            await run_cycle()
        except Exception as e:
            logger.exception(f"Ошибка цикла: {e}")
        await asyncio.sleep(CYCLE_SECONDS)


async def post_init(application):
    async def _notify(text):
        chat = os.getenv("TELEGRAM_CHAT_ID")
        if chat:
            await application.bot.send_message(chat_id=chat, text=text)

    set_notifier(_notify)
    asyncio.create_task(cycle_loop())
    logger.info("Цикл торговли запущен (каждые 5 минут)")


async def cmd_start(update, context):
    await update.message.reply_text("🤖 Капитан Рост на связи! Режим: ТРЕНИРОВКА 🎓 Цикл сканирования идёт каждые 5 минут.")


async def cmd_status(update, context):
    try:
        prices = await market_data.get_tickers()
        eq = paper.equity(prices)

        msg = ["📊 СТАТУС (РЕЖИМ: ТРЕНИРОВКА 🎓)", ""]
        msg.append(f"💰 Свободно: {paper.usdt:.2f} USDT")
        msg.append(f"🏦 Funding: {paper.funding:.2f} USDT")
        msg.append(f"📈 Total Equity: {eq:.2f} $")
        msg.append(f"📦 Позиций: {len(paper.positions)} | Ордеров: {len(paper.orders)}")
        msg.append("")

        for sym, pos in paper.positions.items():
            last = prices.get(sym, {}).get("last", 0)
            pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
            msg.append(f"   {sym}: {pnl_pct:+.1f}% (вход {pos['avg']:.6f})")
        if paper.positions:
            msg.append("")

        wins = [r for r in paper.realized if r["pnl"] > 0]
        losses = [r for r in paper.realized if r["pnl"] <= 0]
        msg.append(f"🧾 Сделок: {len(paper.realized)} (✅ {len(wins)} / ❌ {len(losses)})")
        if paper.realized:
            best = max(paper.realized, key=lambda r: r["pnl_pct"])
            worst = min(paper.realized, key=lambda r: r["pnl_pct"])
            msg.append(f"🏆 Лучшая: {best['symbol']} {best['pnl_pct']:+.1f}%")
            msg.append(f"📉 Худшая: {worst['symbol']} {worst['pnl_pct']:+.1f}%")

        btc = prices.get("BTCUSDT", {}).get("last", 0)
        msg.append("")
        msg.append(f"₿ BTC: {btc:,.0f} $")

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

    app = (Application.builder()
           .token(token)
           .post_init(post_init)
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))

    logger.info("Бот успешно стартовал и слушает Telegram...")
    app.run_polling()


if __name__ == '__main__':
    main()
