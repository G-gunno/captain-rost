import os
import asyncio
import calendar
import html as _html
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp.web as web
from loguru import logger
from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict as TelegramConflict, BadRequest
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.core.orchestrator import run_cycle, set_notifier, CYCLE_SECONDS
from bot.core.state import bot_state
from bot.core.remote_state import ensure_branch
from bot.services.reports import build_report
from bot.strategy.scanner import SCAN_SUMMARY, FILTERED_BY_NEWS, get_regime, threshold
from bot.strategy.learner import learner
from bot.news.cmc import sector_of
from bot.utils.format import fmt_price, fmt_usdt, fmt_pct, fmt_sym

_app = None
WEBHOOK_PATH = "/telegram-webhook"


# ==================== HTML-хелперы ====================
def pnl_emoji(x):
    """Цветной индикатор PnL."""
    return "🟢" if x > 0.05 else ("🔴" if x < -0.05 else "🟡")


async def _safe_send(bot, chat_id, text):
    """HTML с фолбэком на plain text."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
    except BadRequest:
        await bot.send_message(chat_id=chat_id, text=text)


async def reply(update, text):
    try:
        await update.message.reply_text(text, parse_mode="HTML")
    except BadRequest:
        await update.message.reply_text(text)


# ==================== HTTP handlers ====================
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


# ==================== Уведомления и циклы ====================
async def send_chat(text):
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if chat and _app:
        await _safe_send(_app.bot, chat, text)


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


# ==================== ДЕЙСТВИЯ С ПОДТВЕРЖДЕНИЕМ ====================
async def action_pause(context):
    if bot_state.paused:
        return "⏸ Уже на паузе."
    orders = list(paper.orders)
    paper.orders = []
    paper.save()
    bot_state.pause(orders)
    return f"⏸ <b>Пауза</b>: ордеров снято {len(orders)}, позиции открыты."


async def action_resume(context):
    if not bot_state.paused:
        return "▶️ Не на паузе."
    orders = bot_state.resume()
    paper.orders.extend(orders)
    paper.save()
    return f"▶️ <b>Возобновлено</b>: ордеров восстановлено {len(orders)}."


async def action_exitall(context):
    bot_state.trading_enabled = False
    prices = await market_data.get_tickers()
    results = paper.sell_all(prices)
    total = sum(r["pnl"] for r in results)
    return (
        f"🛑 <b>Торговля остановлена</b>\n"
        f"Позиций закрыто: {len(results)} · {pnl_emoji(total)} {total:+.2f} USDT\n"
        f"💰 Баланс: <b>{fmt_usdt(paper.usdt)} USDT</b>\n"
        f"🧠 Опыт обучения сохранён."
    )


async def action_resetlearn(context):
    learner.reset()
    return "🧠♻️ <b>Опыт сброшен</b>: веса = 1.0, история очищена."


async def action_resetstats(context):
    paper.reset_stats()
    learner.reset_stats()
    return (
        "📊 <b>Статистика сброшена</b>\n"
        "PF / DD / Expectancy — с нуля, веса-знания сохранены.\n"
        "Режим вернулся к базовому."
    )


ACTIONS = {
    "pause": (action_pause, "поставить торговлю на паузу?"),
    "resume": (action_resume, "возобновить торговлю?"),
    "exitall": (action_exitall, "продать ВСЕ позиции и остановить торговлю?"),
    "resetlearn": (action_resetlearn, "сбросить опыт обучения (веса и историю)?"),
    "resetstats": (action_resetstats, "сбросить торговую статистику?"),
}


async def ask_confirmation(update, context, key):
    _, question = ACTIONS[key]
    keyboard = [[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{key}"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
    ]]
    await reply(update, f"⚠️ <b>Подтвердите:</b> {_html.escape(question)}")
    # кнопки прикрепляем отдельно, т.к. reply уже отправлен
    await update.message.reply_text("👇", reply_markup=InlineKeyboardMarkup(keyboard))


async def confirm_handler(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        try:
            await query.edit_message_text("❌ Отменено.",
                                          reply_markup=InlineKeyboardMarkup([]))
        except BadRequest:
            pass
        return

    if data.startswith("confirm:"):
        key = data.split(":", 1)[1]
        entry = ACTIONS.get(key)
        if not entry:
            return
        fn, _ = entry
        try:
            result = await fn(context)
            try:
                await query.edit_message_text(
                    f"✅ <b>Подтверждено</b>\n\n{result}",
                    reply_markup=InlineKeyboardMarkup([]),
                )
            except BadRequest:
                await query.edit_message_text(
                    f"✅ Подтверждено\n\n{result}",
                    reply_markup=InlineKeyboardMarkup([]),
                )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
            try:
                await query.edit_message_text(f"⚠️ Ошибка: {e}",
                                              reply_markup=InlineKeyboardMarkup([]))
            except BadRequest:
                pass
        except Exception as e:
            logger.exception(f"confirm action error: {e}")
            try:
                await query.edit_message_text(f"⚠️ Ошибка: {e}",
                                              reply_markup=InlineKeyboardMarkup([]))
            except BadRequest:
                pass


# ==================== Главный запуск ====================
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
        BotCommand("pause", "⏸ Пауза (с подтверждением)"),
        BotCommand("resume", "▶️ Возобновить (с подтверждением)"),
        BotCommand("exitall", "🛑 Продать всё и остановить (с подтверждением)"),
        BotCommand("learn", "🧠 Обучение: веса, winrate, типы выходов"),
        BotCommand("resetlearn", "🧠♻️ Сбросить опыт обучения"),
        BotCommand("resetstats", "📊 Сбросить статистику"),
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


# ==================== Команды Telegram ====================
async def cmd_start(update, context):
    bot_state.fresh_start()
    await reply(update, "🤖 <b>Капитан Рост</b> на связи! Торговля запущена, цикл начат заново.")


async def cmd_help(update, context):
    await reply(update,
        "📖 <b>Мои команды</b>\n"
        "/status — балансы, позиции, метрики\n"
        "/pause · /resume — пауза и возврат\n"
        "/exitall — продать всё и остановить\n"
        "/learn — веса, winrate, сектора, типы выходов\n"
        "/resetstats — сброс статистики (веса сохранятся)\n"
        "/resetlearn — полный сброс обучения\n"
        "/news — новостная аналитика\n"
        "/log — файл лога"
    )


async def cmd_pause(update, context):
    await ask_confirmation(update, context, "pause")


async def cmd_resume(update, context):
    await ask_confirmation(update, context, "resume")


async def cmd_exitall(update, context):
    await ask_confirmation(update, context, "exitall")


async def cmd_resetlearn(update, context):
    await ask_confirmation(update, context, "resetlearn")


async def cmd_resetstats(update, context):
    await ask_confirmation(update, context, "resetstats")


async def cmd_learn(update, context):
    wr, n = learner.winrate()
    lines = ["🧠 <b>Обучение бота</b>", ""]
    lines.append(f"Winrate: <b>{wr:.0%}</b> (последних сделок: {n})")
    lines.append(f"Строгость входа: <b>{learner.threshold_adj:+.1f}</b>")
    lines.append("")
    lines.append("<b>Веса сигналов</b>")
    for k, v in sorted(learner.weights.items(), key=lambda kv: kv[1], reverse=True):
        bar = "▮" * max(1, int(round(v * 5)))
        lines.append(f"   <i>{k}</i>: {v:.2f} {bar}")
    lines.append("")
    lines.append("<b>Типы выходов</b>")
    if learner.exit_stats:
        rows = []
        for t, hist in learner.exit_stats.items():
            if not hist:
                continue
            twr = sum(1 for p in hist if p > 0) / len(hist)
            avg = sum(hist) / len(hist)
            rows.append((t, twr, len(hist), avg))
        rows.sort(key=lambda r: r[3], reverse=True)
        for t, twr, cnt, avg in rows:
            lines.append(f"   {pnl_emoji(avg)} <i>{t}</i>: {cnt} · wr {twr:.0%} · ср. {avg:+.2f}%")
    else:
        lines.append("   (пока нет данных)")
    lines.append("")
    lines.append("<b>Секторная аналитика</b>")
    if learner.sector_stats:
        rows = []
        for s, hist in learner.sector_stats.items():
            if not hist:
                continue
            swr = sum(1 for p in hist if p > 0) / len(hist)
            avg = sum(hist) / len(hist)
            rows.append((s, swr, len(hist), avg, learner.sector_bias(s)))
        rows.sort(key=lambda r: r[1], reverse=True)
        for s, swr, cnt, avg, bias in rows[:8]:
            lines.append(f"   {pnl_emoji(avg)} <i>{s}</i>: wr {swr:.0%} ({cnt}) · ср. {avg:+.2f}% · бонус {bias:+.2f}")
    else:
        lines.append("   (пока нет данных)")
    await reply(update, "\n".join(lines))


async def cmd_news(update, context):
    from bot.news.cmc import get_stats as cmc_stats
    from bot.news.rss_news import get_stats as rss_stats

    cmc = cmc_stats()
    rss = rss_stats()

    lines = ["📰 <b>Новостная аналитика</b>", ""]

    lines.append("📡 <b>RSS-ленты</b>")
    if rss["feeds_working"]:
        lines.append(f"   ✅ работают · {rss['items_count']} новостей")
        lines.append(f"   ⏱ кэш обновлён {rss['cache_age_min']} мин назад")
        if rss["neg_examples"]:
            lines.append(f"   ⚠️ негатив: {_html.escape(rss['neg_examples'][0][:60])}…")
        if rss["pos_examples"]:
            lines.append(f"   ✅ позитив: {_html.escape(rss['pos_examples'][0][:60])}…")
    else:
        lines.append("   ❌ ленты недоступны")

    lines.append("")
    lines.append("🏷 <b>CoinMarketCap</b>")
    lines.append(f"   {'✅' if cmc['api_key_set'] else '❌'} API ключ · 📚 выучено секторов: {cmc['sectors_learned']}")

    lines.append("")
    lines.append("🚫 <b>Отфильтровано новостями</b>")
    if FILTERED_BY_NEWS:
        for item in FILTERED_BY_NEWS[-5:]:
            lines.append(f"   • {_html.escape(fmt_sym(item['symbol']))} · негатив {item['neg_count']}")
    else:
        lines.append("   (пока пусто)")

    await reply(update, "\n".join(lines))


async def cmd_log(update, context):
    src = Path("logs/bot.log")
    if not src.exists():
        await reply(update, "⚠️ Файл лога не найден.")
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
        free_pct = paper.usdt / eq * 100 if eq else 0

        msg = ["📊 <b>Капитан Рост</b> · <i>тренировка</i> 🎓", ""]
        msg.append(f"💰 Свободно: <b>{fmt_usdt(paper.usdt)} USDT</b> ({free_pct:.0f}%)")
        msg.append(f"🏦 Funding: <b>{fmt_usdt(paper.funding)}</b> · 📈 Equity: <b>{fmt_usdt(eq)} $</b>")
        msg.append("")

        if paper.positions:
            invested = sum(
                p["qty"] * prices.get(s, {}).get("last", 0)
                for s, p in paper.positions.items()
            )
            inv_pct = invested / eq * 100 if eq else 0
            msg.append(f"📦 <b>Позиции ({len(paper.positions)})</b> · {inv_pct:.0f}% портфеля")
            for sym, pos in paper.positions.items():
                last = prices.get(sym, {}).get("last", 0)
                w = pos["qty"] * last / eq * 100 if eq else 0
                pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
                kind = "🛰" if pos.get("kind") == "satellite" else "🏛"
                sector = pos.get("sector") or sector_of(sym[:-4])
                tp1 = " · ✅TP1" if pos.get("tp1_done") else ""
                msg.append(f"{kind} <b>{sym[:-4]}</b> · <i>{sector}</i> · {pnl_emoji(pnl_pct)} {fmt_pct(pnl_pct)}{tp1}")
                msg.append(f"   📥 {fmt_price(pos['avg'])} → 📊 {fmt_price(last)}")
                msg.append(f"   🎯 {fmt_price(pos['tp'])} · 🛡 {fmt_price(pos['sl'])} · 💼 {w:.1f}%")
        else:
            msg.append("📦 <b>Позиции</b>: нет")
        msg.append("")

        if paper.orders:
            orders_sum = sum(o["qty"] * o["price"] for o in paper.orders)
            msg.append(f"📋 <b>Ордера ({len(paper.orders)})</b> · {fmt_usdt(orders_sum)} USDT")
            for o in paper.orders:
                w = o["qty"] * o["price"] / eq * 100 if eq else 0
                kind = "🛰" if o.get("kind") == "satellite" else "🏛"
                sector = o.get("sector") or sector_of(o["symbol"][:-4])
                msg.append(f"{kind} <b>{o['symbol'][:-4]}</b> · <i>{sector}</i> · {w:.1f}%")
                msg.append(f"   📥 {fmt_price(o['price'])} · 🎯 {fmt_price(o['tp'])} · 🛡 {fmt_price(o['sl'])}")
        else:
            msg.append("📋 <b>Ордера</b>: нет")
        msg.append("")

        metrics = paper.get_metrics(prices)
        mode, _ = learner.risk_mode(
            metrics["profit_factor"], metrics["max_drawdown_pct"], metrics["total_trades"]
        )
        mode_emoji = {"NORMAL": "🟢", "CAUTIOUS": "🟡", "STRICT": "🔴", "AGGRESSIVE": "🚀"}.get(mode, "⚪")

        msg.append(f"📊 <b>Метрики</b> · {mode_emoji} {mode}")
        msg.append(f"🧾 {metrics['total_trades']} сделок (✅ {metrics['win_count']} / ❌ {metrics['loss_count']})")

        pf = metrics["profit_factor"]
        if pf is None:
            pf_text = "—"
        elif pf == float("inf"):
            pf_text = "∞"
        else:
            pf_text = f"{pf:.2f}"
        dd = metrics["max_drawdown_pct"]
        msg.append(f"📈 PF: <b>{pf_text}</b> · 📉 DD: <b>{dd:.1f}%</b>")

        exp = metrics["expectancy"]
        rf = metrics["recovery_factor"]
        msg.append(f"💹 {pnl_emoji(exp)} {exp:+.2f} · 🔄 RF: {rf:.1f}")

        sat_exposure = sum(
            p["qty"] * prices.get(s, {}).get("last", 0)
            for s, p in paper.positions.items() if p.get("kind") == "satellite"
        ) + sum(
            o["qty"] * o["price"] for o in paper.orders if o.get("kind") == "satellite"
        )
        sat_pct = sat_exposure / eq * 100 if eq else 0
        msg.append(f"🛰 Сателлиты: {sat_pct:.1f}% / 20%")
        msg.append("")

        regime, _ = await get_regime()
        regime_emoji = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}.get(regime, "⚪")
        regime_text = {"bull": "бычий", "neutral": "нейтральный", "bear": "медвежий"}.get(regime, regime)

        btc = prices.get("BTCUSDT", {}).get("last", 0)
        msg.append(f"₿ <b>{fmt_price(btc)} $</b> · {regime_emoji} {regime_text} · 🎯 порог {threshold(regime):g}")
        if SCAN_SUMMARY.get("text"):
            msg.append(f"🔎 {_html.escape(SCAN_SUMMARY['text'])}")
        wr, n = learner.winrate()
        msg.append(f"🧠 wr {wr:.0%} ({n}) · строгость {learner.threshold_adj:+.1f}")

        await reply(update, "\n".join(msg))
    except Exception as e:
        logger.exception("Ошибка в /status")
        await reply(update, f"⚠️ Ошибка: {e}")


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
    app.add_handler(CommandHandler("resetstats", cmd_resetstats))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(confirm_handler))

    logger.info("Бот собран, запускаем webhook-сервер...")
    asyncio.run(run_all(app))


if __name__ == '__main__':
    main()
