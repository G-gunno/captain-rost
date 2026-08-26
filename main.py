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
from telegram.error import Conflict as TelegramConflict
from telegram.ext import Application, CommandHandler

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.core.orchestrator import run_cycle, set_notifier, CYCLE_SECONDS
from bot.core.state import bot_state
from bot.core.remote_state import ensure_branch
from bot.services.reports import build_report
from bot.strategy.scanner import SCAN_SUMMARY
from bot.strategy.learner import learner
from bot.utils.format import fmt_price, fmt_usdt, fmt_pct, fmt_sym

_app = None


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
        try:
            await _app.bot.send_message(chat_id=chat, text=text)
        except Exception as e:
            logger.error(f"send_chat error: {e}")


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


async def error_handler(update, context):
    """Глушим Conflict, чтобы не спамить в логи — Telegram polling продолжит работать."""
    err = context.error
    if isinstance(err, TelegramConflict):
        logger.warning("Telegram Conflict: другой процесс держит polling, ждём и пробуем снова")
        return
    logger.exception(f"Unhandled error: {err}")


async def post_init(application):
    global _app
    _app = application

    # ЖЁСТКАЯ ОЧИСТКА: удаляем любой webhook и сбрасываем очередь — это убирает Conflict
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook удалён, очередь обновлений сброшена")
    except Exception as e:
        logger.error(f"delete_webhook error: {e}")

    set_notifier(send_chat)
    await asyncio.to_thread(ensure_branch)

    await application.bot.set_my_commands([
        BotCommand("start", "🚀 Запустить торговлю"),
        BotCommand("status", "📊 Статус: балансы и позиции"),
        BotCommand("pause", "⏸ Пауза"),
        BotCommand("resume", "▶️ Возобновить"),
        BotCommand("exitall", "🛑 Продать всё и остановить"),
        BotCommand("learn", "🧠 Обучение: веса и winrate"),
        BotCommand("resetlearn", "🧠♻️ Сбросить опыт обучения"),
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
        "/status — балансы, позиции с весами, активные ордера, статистика\n"
        "/pause — пауза (ордера запомнить и снять)\n"
        "/resume — возобновить (ордера вернуть)\n"
        "/exitall — остановить и продать всё (опыт обучения сохраняется)\n"
        "/learn — показать обучение: веса сигналов и winrate\n"
        "/resetlearn — сбросить опыт обучения в ноль\n"
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
        f"Суммарный PnL: {total:+.2f} USDT\nБаланс: {fmt_usdt(paper.usdt)} USDT\n"
        f"🧠 Опыт обучения сохранён."
    )


async def cmd_learn(update, context):
    wr, n = learner.winrate()
    lines = ["🧠 ОБУЧЕНИЕ БОТА", ""]
    lines.append(f"Winrate: {wr:.0%} (последних сделок: {n})")
    lines.append(f"Строгость входа: +{learner.threshold_adj}")
    lines.append("")
    lines.append("Веса сигналов:")
    for k, v in sorted(learner.weights.items(), key=lambda kv: kv[1], reverse=True):
        bar = "▮" * max(1, int(round(v * 5)))
        lines.append(f"   {k}: {v:.2f} {bar}")
    await update.message.reply_text("\n".join(lines))


async def cmd_resetlearn(update, context):
    learner.reset()
    await update.message.reply_text("🧠♻️ Опыт обучения сброшен: все веса = 1.0, история очищена.")


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
        free_pct = paper.usdt / eq * 100 if eq else 0
        msg.append(f"💰 Свободно: {fmt_usdt(paper.usdt)} USDT ({free_pct:.0f}%)")
        msg.append(f"🏦 Funding: {fmt_usdt(paper.funding)} USDT")
        msg.append(f"📈 Total Equity: {fmt_usdt(eq)} $")

        msg.append("")
        if paper.positions:
            invested = sum(
                p["qty"] * prices.get(s, {}).get("last", 0)
                for s, p in paper.positions.items()
            )
            inv_pct = invested / eq * 100 if eq else 0
            msg.append(
                f"📦 ПОЗИЦИИ ({len(paper.positions)}) · "
                f"занято {fmt_usdt(invested)} USDT ({inv_pct:.0f}% портфеля):"
            )
            for sym, pos in paper.positions.items():
                last = prices.get(sym, {}).get("last", 0)
                val = pos["qty"] * last
                w = val / eq * 100 if eq else 0
                pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
                tp1 = " · TP1✅" if pos.get("tp1_done") else ""
                msg.append(f"   • {fmt_sym(sym)} · {fmt_pct(pnl_pct)}{tp1}")
                msg.append(f"      💼 Вес: {fmt_usdt(val)} USDT ({w:.1f}% портфеля)")
                msg.append(f"      📥 {fmt_price(pos['avg'])} → 📊 {fmt_price(last)}")
                msg.append(f"      🎯 {fmt_price(pos['tp'])} | 🛡 {fmt_price(pos['sl'])}")
        else:
            msg.append("📦 ПОЗИЦИИ: нет")

        msg.append("")
        if paper.orders:
            orders_sum = sum(o["qty"] * o["price"] for o in paper.orders)
            msg.append(
                f"📋 АКТИВНЫЕ ОРДЕРА ({len(paper.orders)}) · "
                f"на {fmt_usdt(orders_sum)} USDT:"
            )
            for o in paper.orders:
                val = o["qty"] * o["price"]
                w = val / eq * 100 if eq else 0
                msg.append(
                    f"   • {fmt_sym(o['symbol'])}: {fmt_usdt(val)} USDT "
                    f"({w:.1f}%) @ {fmt_price(o['price'])}"
                )
                msg.append(f"      🎯 {fmt_price(o['tp'])} | 🛡 {fmt_price(o['sl'])}")
        else:
            msg.append("📋 АКТИВНЫЕ ОРДЕРА: нет")

        msg.append("")
        wins = [r for r in paper.realized if r["pnl"] > 0]
        losses = [r for r in paper.realized if r["pnl"] <= 0]
        msg.append(f"🧾 Сделок: {len(paper.realized)} (✅ {len(wins)} / ❌ {len(losses)})")
        if paper.realized:
            best = max(paper.realized, key=lambda r: r["pnl_pct"])
            worst = min(paper.realized, key=lambda r: r["pnl_pct"])
            msg.append(f"🏆 Лучшая: {fmt_sym(best['symbol'])} {fmt_pct(best['pnl_pct'])}")
            msg.append(f"📉 Худшая: {fmt_sym(worst['symbol'])} {fmt_pct(worst['pnl_pct'])}")

        btc = prices.get("BTCUSDT", {}).get("last", 0)
        msg.append("")
        msg.append(f"₿ BTC: {fmt_price(btc)} $")
        if SCAN_SUMMARY.get("text"):
            msg.append(f"🔎 Сканирование: {SCAN_SUMMARY['text']}")
        msg.append(f"🧠 Обучение: {learner.summary()}")

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

    app.add_error_handler(error_handler)  # глушим Conflict в логах

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("exitall", cmd_exitall))
    app.add_handler(CommandHandler("learn", cmd_learn))
    app.add_handler(CommandHandler("resetlearn", cmd_resetlearn))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("help", cmd_help))

    logger.info("Бот успешно стартовал и слушает Telegram...")
    # drop_pending_updates + delete_webhook в post_init = финальная защита от Conflict
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
