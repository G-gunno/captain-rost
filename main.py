import os
import asyncio
import calendar
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp.web as web
from loguru import logger
from telegram import BotCommand, Update
from telegram.error import Conflict as TelegramConflict
from telegram.ext import Application, CommandHandler

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.core.orchestrator import run_cycle, set_notifier, CYCLE_SECONDS
from bot.core.state import bot_state
from bot.core.remote_state import ensure_branch
from bot.services.reports import build_report
from bot.strategy.scanner import SCAN_SUMMARY, FILTERED_BY_NEWS, get_regime
from bot.strategy.learner import learner
from bot.utils.format import fmt_price, fmt_usdt, fmt_pct, fmt_sym

_app = None
WEBHOOK_PATH = "/telegram-webhook"


async def health_handler(request):
    return web.Response(text="OK")


async def webhook_handler(request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot=_app.bot)
        await _app.process_update(update)
    except Exception as e:
        logger.error(f"webhook process_update error: {e}")
    return web.Response(text="OK")


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
    err = context.error
    if isinstance(err, TelegramConflict):
        logger.warning("Telegram Conflict (вебхук-режим, игнорируем)")
        return
    logger.exception(f"Unhandled error: {err}")


async def run_all(application):
    global _app
    _app = application

    await application.initialize()
    await application.start()

    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook удалён, очередь обновлений сброшена")
    except Exception as e:
        logger.error(f"delete_webhook error: {e}")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")
    webhook_url = f"{public_url}{WEBHOOK_PATH}"

    await application.bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True,
        allowed_updates=["message", "edited_message", "callback_query"],
    )
    logger.info(f"✅ Webhook установлен: {webhook_url}")

    await application.bot.set_my_commands([
        BotCommand("start", "🚀 Запустить торговлю"),
        BotCommand("status", "📊 Статус: балансы и позиции"),
        BotCommand("pause", "⏸ Пауза"),
        BotCommand("resume", "▶️ Возобновить"),
        BotCommand("exitall", "🛑 Продать всё и остановить"),
        BotCommand("learn", "🧠 Обучение: веса и winrate"),
        BotCommand("resetlearn", "🧠♻️ Сбросить опыт обучения"),
        BotCommand("news", "📰 Статус новостной аналитики"),
        BotCommand("log", "📄 Файл лога"),
        BotCommand("help", "📖 Справка"),
    ])

    await asyncio.to_thread(ensure_branch)

    set_notifier(send_chat)
    asyncio.create_task(cycle_loop())
    asyncio.create_task(report_loop())
    logger.info("Цикл торговли и отчёты запущены (WEBHOOK MODE)")

    web_app = web.Application()
    web_app.router.add_get("/", health_handler)
    web_app.router.add_post(WEBHOOK_PATH, webhook_handler)

    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"HTTP-сервер запущен на порту {port} (GET / + POST {WEBHOOK_PATH})")

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("Остановка...")
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.stop()
        await application.shutdown()
        await runner.cleanup()


async def cmd_start(update, context):
    bot_state.fresh_start()
    await update.message.reply_text(
        "🤖 Капитан Рост на связи! Торговля запущена, цикл начат заново, временный файл ордеров удалён."
    )


async def cmd_help(update, context):
    await update.message.reply_text(
        "📖 МОИ КОМАНДЫ:\n"
        "/start — запустить торговлю, новый цикл\n"
        "/status — балансы, позиции с весами, активные ордера, метрики, статистика\n"
        "/pause — пауза (ордера запомнить и снять)\n"
        "/resume — возобновить (ордера вернуть)\n"
        "/exitall — остановить и продать всё (опыт обучения сохраняется)\n"
        "/learn — показать обучение: веса сигналов и winrate\n"
        "/resetlearn — сбросить опыт обучения в ноль\n"
        "/news — статус новостной аналитики (CMC + RSS)\n"
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


async def cmd_news(update, context):
    from bot.news.cmc import get_stats as cmc_stats
    from bot.news.rss_news import get_stats as rss_stats

    cmc = cmc_stats()
    rss = rss_stats()

    lines = ["📰 НОВОСТНАЯ АНАЛИТИКА", ""]

    lines.append("📡 RSS-ЛЕНТЫ:")
    if rss["feeds_working"]:
        lines.append(f"   ✅ Работают · {rss['items_count']} новостей")
        lines.append(f"   ⏱ Кэш обновлён {rss['cache_age_min']} мин назад")
        if rss["neg_examples"]:
            lines.append(f"   ⚠️ Негатив: {rss['neg_examples'][0][:60]}...")
        if rss["pos_examples"]:
            lines.append(f"   ✅ Позитив: {rss['pos_examples'][0][:60]}...")
    else:
        lines.append("   ❌ Ленты недоступны (проверь сеть)")

    lines.append("")
    lines.append("🏷 COINMARKETCAP:")
    lines.append(f"   {'✅' if cmc['api_key_set'] else '❌'} API ключ: {'настроен' if cmc['api_key_set'] else 'НЕ НАСТРОЕН'}")
    lines.append(f"   📊 В кэше: {cmc['cache_count']} монет")

    lines.append("")
    lines.append("🚫 ОТФИЛЬТРОВАНО (последние 5):")
    if FILTERED_BY_NEWS:
        for item in FILTERED_BY_NEWS[-5:]:
            lines.append(f"   • {fmt_sym(item['symbol'])} (негатив {item['neg_count']})")
    else:
        lines.append("   (пока ничего не отфильтровано)")

    await update.message.reply_text("\n".join(lines))


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
        metrics = paper.get_metrics(prices)
        mode, _ = learner.risk_mode(metrics["profit_factor"], metrics["max_drawdown_pct"])
        mode_emoji = {"NORMAL": "🟢", "CAUTIOUS": "🟡", "STRICT": "🔴", "AGGRESSIVE": "🚀"}.get(mode, "⚪")

        msg.append(f"📊 МЕТРИКИ · режим {mode_emoji} {mode}")
        msg.append(f"   🧾 Сделок: {metrics['total_trades']} (✅ {metrics['win_count']} / ❌ {metrics['loss_count']})")
        
        pf = metrics["profit_factor"]
        if pf is None:
            pf_text = "—"
        elif pf == float("inf"):
            pf_text = "∞"
        else:
            pf_text = f"{pf:.2f}"
            if pf < 1.0:
                pf_text += " ❌"
            elif pf < 1.3:
                pf_text += " ⚠️"
            else:
                pf_text += " 🎯"
        msg.append(f"   📈 Profit Factor: {pf_text}  (цель ≥ 1.3)")
        
        dd = metrics["max_drawdown_pct"]
        if dd < 5:
            dd_text = f"{dd:.1f}% ✅"
        elif dd < 15:
            dd_text = f"{dd:.1f}% ⚠️"
        else:
            dd_text = f"{dd:.1f}% 🔴"
        msg.append(f"   📉 Max Drawdown: {dd_text}  (лимит 15%)")
        
        # Expectancy
        exp = metrics["expectancy"]
        if exp > 0:
            exp_text = f"{exp:+.2f} USDT 🎯"
        else:
            exp_text = f"{exp:+.2f} USDT ❌"
        msg.append(f"   💹 Expectancy: {exp_text}  (мат. ожидание на сделку)")
        
        # Recovery Factor
        rf = metrics["recovery_factor"]
        if rf > 2:
            rf_text = f"{rf:.1f} 🎯"
        elif rf > 1:
            rf_text = f"{rf:.1f} ⚠️"
        else:
            rf_text = f"{rf:.1f} ❌"
        msg.append(f"   🔄 Recovery Factor: {rf_text}  (скорость восстановления)")
        
        msg.append(f"   💵 Суммарный PnL: {metrics['total_pnl']:+.2f} USDT")

        if paper.realized:
            best = max(paper.realized, key=lambda r: r["pnl_pct"])
            worst = min(paper.realized, key=lambda r: r["pnl_pct"])
            msg.append(f"   🏆 Лучшая: {fmt_sym(best['symbol'])} {fmt_pct(best['pnl_pct'])}")
            msg.append(f"   📉 Худшая: {fmt_sym(worst['symbol'])} {fmt_pct(worst['pnl_pct'])}")

        regime, _ = await get_regime()
        regime_emoji = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}.get(regime, "⚪")
        regime_text = {"bull": "Бычий", "neutral": "Нейтральный", "bear": "Медвежий"}.get(regime, regime)

        btc = prices.get("BTCUSDT", {}).get("last", 0)
        msg.append("")
        msg.append(f"₿ BTC: {fmt_price(btc)} $ · {regime_emoji} {regime_text}")
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
    logger.info("Запуск бота CaptainRost (PAPER MODE, WEBHOOK)...")

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("Ошибка! TELEGRAM_BOT_TOKEN не найден.")
        return

    app = Application.builder().token(token).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("exitall", cmd_exitall))
    app.add_handler(CommandHandler("learn", cmd_learn))
    app.add_handler(CommandHandler("resetlearn", cmd_resetlearn))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("help", cmd_help))

    logger.info("Бот собран, запускаем webhook-сервер...")
    asyncio.run(run_all(app))


if __name__ == '__main__':
    main()
