import os
import asyncio
import calendar
import threading
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from zoneinfo import ZoneInfo

from loguru import logger
from telegram import BotCommand
from telegram.ext import Application, CommandHandler

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.core.orchestrator import run_cycle, set_notifier, CYCLE_SECONDS
from bot.core.state import bot_state
from bot.services.reports import build_report

_app = None


# --- Мини веб-сервер "пульс" для Render (чтобы бесплатный тариф не засыпал) ---
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


async def send_chat(text):
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if chat and _app:
        await _app.bot.send_message(chat_id=chat, text=text)


async def cycle_loop():
    await asyncio.sleep(15)
    while True:
        try:
            await run_cycle()
        except Exception as e:
            logger.exception(f"Ошибка цикла: {e}")
        await asyncio.sleep(CYCLE_SECONDS)


async def report_loop():
    tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
    last_sent = None
    while True:
        try:
            now = datetime.now(tz)
            if now.hour == 21 and now.minute < 5 and last_sent != now.date():
                last_sent = now.date()
                await send_chat(await build_report("daily", tz))
                if now.weekday() == 6:
                    await send_chat(await build_report("weekly", tz))
                if now.day == calendar.monthrange(now.year, now.month)[1]:
                    await send_chat(await build_report("monthly", tz))
        except Exception as e:
            logger.exception(f"report error: {e}")
        await asyncio.sleep(30)


async def post_init(application):
    global _app
    _app = application
    set_notifier(send_chat)

    # Регистрируем меню команд в Telegram
    await application.bot.set_my_commands([
        BotCommand("start", "🚀 Запустить торговлю"),
        BotCommand("status", "📊 Статус: балансы и позиции"),
        BotCommand("pause", "⏸ Пауза"),
        BotCommand("resume", "▶️ Возобновить"),
        BotCommand("exitall", "🛑 Продать всё и остановить"),
        BotCommand("log", "📄 Файл лога"),
        BotCommand("help", "📖 Справка"),
    ])

    asyncio.create_task(cycle_loop())
    asyncio.create_task(report_loop())
    logger.info("Цикл торговли и отчёты запущены")


# --- Команды Telegram ---
async def cmd_start(update, context):
    bot_state.fresh_start()
    await update.message.reply_text(
        "🤖 Капитан Рост на связи! Торговля запущена, цикл начат заново, временный файл ордеров удалён."
    )


async def cmd_help(update, context):
    await update.message.reply_text(
        "📖 МОИ КОМАНДЫ:\n"
        "/start — запустить торговлю, новый цикл\n"
        "/status — балансы, позиции, статистика сделок\n"
        "/pause — пауза (ордера запомнить и снять)\n"
        "/resume — возобновить (ордера вернуть)\n"
        "/exitall — остановить и продать всё\n"
        "/log — прислать файл лога\n"
        "/help — эта справка"
    )


async def cmd_pause(update, context):
    if bot_state.paused:
        await update.message.reply_text("⏸ Уже на паузе.")
        return
    orders = list(paper.orders)
    paper.orders = []
    paper.save()
    bot_state.pause(orders)
    await update.message.reply_text(
        f"⏸ ПАУЗА. Ордеров запомнено и снято: {len(orders)}. Открытые позиции остались."
    )


async def cmd_resume(update, context):
    if not bot_state.paused:
        await update.message.reply_text("▶️ Не на паузе.")
        return
    orders = bot_state.resume()
    paper.orders.extend(orders)
    paper.save()
    await update.message.reply_text(f"▶️ ВОЗОБНОВЛЕНО. Ордеров восстановлено: {len(orders)}.")


async def cmd_exitall(update, context):
    bot_state.trading_enabled = False
    prices = await market_data.get_tickers()
    results = paper.sell_all(prices)
    total = sum(r["pnl"] for r in results)
    await update.message.reply_text(
        f"🛑 ТОРГОВЛЯ ОСТАНОВЛЕНА.\nПозиций закрыто: {len(results)}\n"
        f"Суммарный PnL: {total:+.2f} USDT\nБаланс: {paper.usdt:.2f} USDT"
    )


async def cmd_log(update, context):
    src = Path("logs/bot.log")
    if not src.exists():
        await update.message.reply_text("⚠️ Файл лога не найден.")
        return
    name = f"log_{datetime.now().strftime('%H%M%S')}.txt"
    tmp = Path("logs") / name
    tmp.write_bytes(src.read_bytes())
    with open(tmp, "rb") as f:
        await update.message.reply_document(document=f, filename=name)


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
    os.makedirs("logs", exist_ok=True)
    logger.add("logs/bot.log", rotation="5 MB", retention="7 days", enqueue=True, level="INFO")
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
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("exitall", cmd_exitall))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("help", cmd_help))

    logger.info("Бот успешно стартовал и слушает Telegram...")
    app.run_polling()


if __name__ == '__main__':
    main()
