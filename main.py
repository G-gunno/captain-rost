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
from bot.services.info import info_full_text
from bot.strategy.shadow import shadow
from bot.strategy.scanner import SCAN_SUMMARY, FILTERED_BY_NEWS, get_regime, threshold
from bot.strategy.learner import learner, TIERS
from bot.news.cmc import sector_of, TIER_EMOJI, TIER_NAMES, memory_stats
from bot.utils.format import fmt_price, fmt_pct, fmt_sym

_app = None
WEBHOOK_PATH = "/telegram-webhook"


# ==================== Хелперы ====================
from functools import wraps

def restricted(func):
    """Декоратор для блокировки доступа чужим пользователям."""
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        user_chat_id = str(update.effective_chat.id)
        admin_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if user_chat_id != admin_chat_id:
            logger.warning(f"🚨 Попытка взлома! Заблокирован доступ от чата: {user_chat_id}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def usd(x):
    return f"${x:,.2f}"

def pnl_emoji(x):
    return "🟢" if x > 0.05 else ("🔴" if x < -0.05 else "🟡")

def weight_emoji(v):
    return "🔥" if v >= 1.1 else ("🟢" if v >= 0.9 else ("🟡" if v >= 0.7 else "🔻"))

# --- НОВЫЙ ЕДИНЫЙ СТАНДАРТ ОФОРМЛЕНИЯ МОНЕТ ---
def format_coin(sym, data_obj):
    kind = "🛰" if data_obj.get("kind") == "satellite" else "🏛"
    mode = "🚀" if data_obj.get("is_momentum") else "🏹"
    tier = data_obj.get("tier")
    em = TIER_EMOJI.get(tier, "") if tier else ""
    sector = data_obj.get("sector") or "Other"
    
    base_sym = sym[:-4] if sym.endswith("USDT") else sym
    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")
    chart_url = f"{public_url}/chart?symbol={sym}"
    
    return f"{mode} {kind} <a href='{chart_url}'><b>{base_sym}</b></a>{' ' + em if em else ''} · <i>{sector}</i>"


async def reply(update, text, markup=None):
    try:
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True
        )
    except BadRequest:
        await update.message.reply_text(
            text, reply_markup=markup, disable_web_page_preview=True
        )


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


async def chart_handler(request):
    symbol = request.query.get("symbol")
    if not symbol:
        return web.Response(text="Укажите тикер, например ?symbol=LINKUSDT", status=400)
        
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    def make_chart():
        try:
            from bot.utils.visualizer import TradeVisualizer
            viz = TradeVisualizer(log_path="logs/bot.log", symbol=symbol)
            fig = viz.build_chart(show=False) 
            if fig is None:
                return None
            return fig.to_html(include_plotlyjs="cdn", full_html=True)
        except Exception as e:
            logger.error(f"Visualizer error: {e}")
            return str(e)

    try:
        html_or_error = await asyncio.to_thread(make_chart)
        if html_or_error is None:
            return web.Response(text=f"Нет данных лога или свечей для {symbol}. Возможно, бот её еще не торговал.", status=404)
        if not html_or_error.startswith("<"):
             return web.Response(text=f"Ошибка генерации: {html_or_error}", status=500)
        return web.Response(text=html_or_error, content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Внутренняя ошибка сервера: {e}", status=500)


# ==================== Уведомления и циклы ====================
async def send_chat(text):
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if chat and _app:
        try:
            await _app.bot.send_message(
                chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True
            )
        except BadRequest:
            await _app.bot.send_message(
                chat_id=chat, text=text, disable_web_page_preview=True
            )


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
        f"Позиций закрыто: {len(results)} · {pnl_emoji(total)} {total:+.2f}%\n"
        f"💰 Баланс: <b>{usd(paper.usdt)}</b>\n"
        f"🧠 Опыт обучения сохранён."
    )


async def action_resetlearn(context):
    learner.reset()
    shadow.reset()
    return (
        "🧠♻️ <b>Опыт ИИ сброшен</b>\n"
        "• Веса индикаторов возвращены к 1.0\n"
        "• Теневой журнал автотюна полностью очищен."
    )


async def action_resetstats(context):
    paper.reset_stats()
    learner.reset_stats()
    return (
        "📊 <b>Статистика и балансы сброшены</b>\n"
        "PF / DD / Expectancy — с нуля, веса-знания сохранены.\n"
        "💰 Баланс: <b>$1,000.00</b>\n"
        "📦 Все ордера и позиции очищены."
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
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm:{key}"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
    ]])
    await reply(update, f"⚠️ <b>Подтвердите:</b> {_html.escape(question)}", markup=keyboard)


@restricted
async def confirm_handler(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "cancel":
        try:
            await query.edit_message_text("❌ Отменено.", reply_markup=InlineKeyboardMarkup([]))
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
                await query.edit_message_text(f"✅ <b>Подтверждено</b>\n\n{result}", reply_markup=InlineKeyboardMarkup([]))
            except BadRequest:
                await query.edit_message_text(f"✅ Подтверждено\n\n{result}", reply_markup=InlineKeyboardMarkup([]))
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
            try:
                await query.edit_message_text(f"⚠️ Ошибка: {e}", reply_markup=InlineKeyboardMarkup([]))
            except BadRequest:
                pass
        except Exception as e:
            logger.exception(f"confirm action error: {e}")


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
        url=webhook_url, drop_pending_updates=True,
        allowed_updates=["message", "edited_message", "callback_query"],
    )
    logger.info(f"✅ Webhook установлен: {webhook_url}")

    await application.bot.set_my_commands([
        BotCommand("start", "🚀 Запустить торговлю"),
        BotCommand("pause", "⏸ Пауза (с подтверждением)"),
        BotCommand("resume", "▶️ Возобновить (с подтверждением)"),
        BotCommand("status", "📊 Статус: балансы и позиции"),
        BotCommand("chart", "📈 График монеты (сделки и отмены)"),
        BotCommand("learn", "🧠 Обучение: параметры, сектора, веса, память"),
        BotCommand("news", "📰 Статус новостной аналитики"),
        BotCommand("exitall", "🛑 Продать всё и остановить (с подтверждением)"),
        BotCommand("resetstats", "📊 Сбросить статистику"),
        BotCommand("resetlearn", "🧠♻️ Сбросить опыт обучения"),
        BotCommand("log", "📄 Файл лога"),
        BotCommand("autotune", "🎛 Автотюн: статус и вкл/выкл"),
        BotCommand("info", "📖 Информация о боте"),
        BotCommand("help", "📖 Справка"),
    ])

    await asyncio.to_thread(ensure_branch)
    set_notifier(send_chat)
    
# === ЗАПУСК НОВОЙ EVENT-DRIVEN АРХИТЕКТУРЫ ===
    from bot.core.event_bus import EventBus
    from bot.workers.market_data import MarketDataWorker
    from bot.workers.execution import ExecutionRiskWorker
    from bot.workers.scanner import ScannerWorker
    from bot.workers.order_manager import OrderManagerWorker
    from bot.workers.notification import NotificationWorker

    global_bus = EventBus()
    
    # Инициализация воркеров
    md_worker = MarketDataWorker(global_bus)
    exec_worker = ExecutionRiskWorker(global_bus)
    scanner_worker = ScannerWorker(global_bus)
    order_manager_worker = OrderManagerWorker(global_bus)
    notify_worker = NotificationWorker(global_bus, send_chat_func=send_chat)
    
    workers = [
        asyncio.create_task(md_worker.run(), name="Worker-MarketData"),
        asyncio.create_task(exec_worker.run(), name="Worker-Execution"),
        asyncio.create_task(scanner_worker.run(), name="Worker-Scanner"),
        asyncio.create_task(order_manager_worker.run(), name="Worker-OrderManager"),
        asyncio.create_task(notify_worker.run(), name="Worker-Notification"),
    ]

    def worker_callback(t: asyncio.Task):
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"Воркер {t.get_name()} завершился с ошибкой: {e}")

    for task in workers:
        task.add_done_callback(worker_callback)
    # ====================================================================

    global_bus = EventBus()
    
    # Инициализация воркеров
    md_worker = MarketDataWorker(global_bus)
    exec_worker = ExecutionRiskWorker(global_bus)
    
    # В Python 3.11+ предпочтительнее использовать asyncio.TaskGroup, 
    # но для обратной совместимости оставляем asyncio.create_task:
    workers = [
        asyncio.create_task(md_worker.run(), name="Worker-MarketData"),
        asyncio.create_task(exec_worker.run(), name="Worker-Execution"),
        # asyncio.create_task(scanner_worker.run(), name="Worker-Scanner"),
    ]

    # Если какой-то воркер упадет, мы должны об этом узнать
    def worker_callback(t: asyncio.Task):
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"Воркер {t.get_name()} завершился с ошибкой: {e}")

    for task in workers:
        task.add_done_callback(worker_callback)

    # ====================================================================

    # Сохраняем старые циклы (на период переходного этапа)
    asyncio.create_task(cycle_loop())
    asyncio.create_task(report_loop())

    # ... запуск веб-сервера aiohttp и graceful shutdown ...
    
    from bot.exchange.market_data import start_ws_ticker_stream
    asyncio.create_task(start_ws_ticker_stream())
    
    from bot.exchange.market_data import start_ws_ticker_stream
    asyncio.create_task(start_ws_ticker_stream())
    
    from bot.strategy.fundamental import update_fundamental_data
    asyncio.create_task(update_fundamental_data())
    
    logger.info("Цикл торговли, отчёты и Макро-дата запущены (WEBHOOK MODE)")

    web_app = web.Application()
    web_app.router.add_get("/", health_handler)
    web_app.router.add_get("/chart", chart_handler)
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
@restricted
async def cmd_start(update, context):
    bot_state.fresh_start()
    await reply(update, "🤖 <b>Капитан Рост</b> на связи! Торговля запущена, цикл начат заново.")


@restricted
async def cmd_info(update, context):
    from bot.services.info import info_full_text, generate_whitepaper
    import io
    await reply(update, info_full_text())

    whitepaper_text = generate_whitepaper()
    doc = io.BytesIO(whitepaper_text.encode('utf-8'))
    doc.name = "CaptainRost_Whitepaper.txt"
    
    await update.message.reply_document(
        document=doc,
        caption="📄 <b>Подробная документация (Whitepaper)</b>\nПолное описание архитектуры, формул и логики бота.",
        parse_mode="HTML"
    )


@restricted
async def cmd_help(update, context):
    await reply(update,
        "📖 <b>Мои команды</b>\n"
        "/status — позиции и метрики\n"
        "/info — информация о боте\n"
        "/learn — обучение\n"
        "/news — новостная аналитика\n"
        "/pause, /resume, /exitall — с подтверждением\n"
        "/resetstats, /resetlearn — сбросы\n"
        "/log — файл лога"
    )

@restricted
async def cmd_pause(update, context): await ask_confirmation(update, context, "pause")

@restricted
async def cmd_resume(update, context): await ask_confirmation(update, context, "resume")

@restricted
async def cmd_exitall(update, context): await ask_confirmation(update, context, "exitall")

@restricted
async def cmd_resetlearn(update, context): await ask_confirmation(update, context, "resetlearn")

@restricted
async def cmd_resetstats(update, context): await ask_confirmation(update, context, "resetstats")


@restricted
async def cmd_learn(update, context):
    wr, n = learner.winrate()
    regime, _ = await get_regime()
    lines = ["🧠 <b>Обучение бота (ИИ)</b>", ""]

    lines.append("📌 <b>Текущие параметры</b>")
    lines.append(
        f"🎯 Winrate: <b>{wr:.0%}</b> <i>(за {n} сдел.)</i> · строгость: <b>{learner.threshold_adj:+.1f}</b> · "
        f"порог: <b>{threshold(regime):g}</b>"
    )
    lines.append(
        f"🛰 Сателлиты: лимит <b>{learner.satellite_limit():.0f}%</b> · "
        f"размер <b>{learner.satellite_size_pct():.1f}%</b>"
    )
    
    core_hist = learner.kind_stats.get("core") or []
    sat_hist = learner.kind_stats.get("satellite") or []
    if core_hist or sat_hist:
        lines.append("")
        lines.append("🏛/🛰 <b>Стиль торговли</b> <i>(последние 50 сдел.)</i>")
        if core_hist:
            cwr = sum(1 for p in core_hist if p > 0) / len(core_hist)
            cavg = sum(core_hist) / len(core_hist)
            lines.append(f"   🏛 Core: {len(core_hist)} сдел. · wr {cwr:.0%} · ср. {cavg:+.2f}%")
        if sat_hist:
            swr = sum(1 for p in sat_hist if p > 0) / len(sat_hist)
            savg = sum(sat_hist) / len(sat_hist)
            lines.append(f"   🛰 Сателлиты: {len(sat_hist)} сдел. · wr {swr:.0%} · ср. {savg:+.2f}%")

    lines.append("")
    lines.append("🏹/🚀 <b>Стратегии входа</b> <i>(последние 50 сдел.)</i>")
    rock_hist = learner.entry_stats.get("rocket") or []
    snip_hist = learner.entry_stats.get("sniper") or []
    
    if rock_hist:
        r_wr = sum(1 for p in rock_hist if p > 0) / len(rock_hist)
        r_avg = sum(rock_hist) / len(rock_hist)
        lines.append(f"   🚀 Ракеты (пробой): {len(rock_hist)} сдел. · wr {r_wr:.0%} · ср. {r_avg:+.2f}%")
    if snip_hist:
        s_wr = sum(1 for p in snip_hist if p > 0) / len(snip_hist)
        s_avg = sum(snip_hist) / len(snip_hist)
        lines.append(f"   🏹 Снайпер (откат): {len(snip_hist)} сдел. · wr {s_wr:.0%} · ср. {s_avg:+.2f}%")
    if not rock_hist and not snip_hist:
         lines.append("   (накапливается)")
    
    lines.append("")
    lines.append("🧭 <b>Где деньги</b> · сектора и тиры <i>(последние 50 сдел.)</i>")
    if learner.sector_stats:
        rows = []
        for s, hist in learner.sector_stats.items():
            if not hist:
                continue
            swr = sum(1 for p in hist if p > 0) / len(hist)
            avg = sum(hist) / len(hist)
            rows.append((s, swr, len(hist), avg, learner.sector_bias(s)))
        rows.sort(key=lambda r: r[4], reverse=True)
        for s, swr, cnt, avg, bias in rows:
            lines.append(
                f"   {pnl_emoji(avg)} <i>{s}</i> · wr {swr:.0%} ({cnt} сдел.) · "
                f"{avg:+.2f}% → бонус {bias:+.2f}"
            )
    else:
        lines.append("   (пока нет данных по секторам)")
        
    tier_shown = False
    for t in TIERS:
        hist = learner.tier_stats.get(t) or []
        if not hist:
            continue
        tier_shown = True
        twr = sum(1 for p in hist if p > 0) / len(hist)
        tavg = sum(hist) / len(hist)
        lines.append(
            f"   {TIER_EMOJI[t]} <i>{TIER_NAMES[t]}</i> · wr {twr:.0%} ({len(hist)} сдел.) · "
            f"{tavg:+.2f}% → бонус {learner.tier_bias(t):+.2f}"
        )
    if not tier_shown:
        lines.append("   🐘 Кап-тиры: накапливается")

    lines.append("")
    lines.append("🎯 <b>Чему верит бот</b> · веса сигналов <i>(накопительно)</i>")
    for k, v in sorted(learner.weights.items(), key=lambda kv: kv[1], reverse=True):
        bar = "⚡" * max(1, int(round(v * 5)))
        lines.append(f"   {weight_emoji(v)} <i>{k}</i> · {v:.2f} {bar}")

    lines.append("")
    lines.append("🧾 <b>Как выходим</b> · типы выходов <i>(последние 50 сдел.)</i>")
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
            lines.append(f"   {pnl_emoji(avg)} <i>{t}</i> · {cnt} сдел. · wr {twr:.0%} · {avg:+.2f}%")
    else:
        lines.append("   (пока нет данных)")
        
    lines.append("")
    mem = memory_stats()
    lines.append("🗂 <b>Память по монетам</b>")
    lines.append(
        f"   📚 База {mem['base']} + выучено {mem['learned']} = "
        f"<b>{mem['total']}</b> монет"
    )
    sec_txt = " | ".join(
        f"{s} · {c}" for s, c in sorted(mem["sectors"].items(), key=lambda kv: kv[1], reverse=True)
    )
    lines.append(f"   🧭 {sec_txt}")
    tier_txt = " · ".join(
        f"{TIER_EMOJI[t]} {c}" for t, c in sorted(mem["tiers"].items(), key=lambda kv: kv[1], reverse=True)
    )
    lines.append(f"   🏆 {tier_txt}")
    
    lines.append("")
    lines.extend(shadow.learn_lines())

    await reply(update, "\n".join(lines))


@restricted
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
    lines.append(f"   {'✅' if cmc['api_key_set'] else '❌'} API ключ · 📚 секторов: {cmc['sectors_learned']} · 🏆 рангов: {cmc['ranks_cached']}")

    lines.append("")
    lines.append("🚫 <b>Отфильтровано новостями</b>")
    if FILTERED_BY_NEWS:
        for item in FILTERED_BY_NEWS[-5:]:
            lines.append(f"   • {_html.escape(fmt_sym(item['symbol']))} · негатив {item['neg_count']}")
    else:
        lines.append("   (пока пусто)")

    await reply(update, "\n".join(lines))


@restricted
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

@restricted
async def cmd_chart(update, context):
    import time
    arg = (context.args or [None])[0]
    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")

    if not arg:
        now_ts = int(time.time())
        cutoff = now_ts - 86400
        symbols = set()

        for sym in paper.positions.keys():
            symbols.add(sym)
            
        for o in paper.orders:
            symbols.add(o["symbol"])

        for t in paper.trades:
            if t.get("time", 0) >= cutoff:
                symbols.add(t["symbol"])

        if not symbols:
            await reply(update, "⚠️ За последние 24 часа активности не было. Укажите тикер вручную: <code>/chart LINK</code>")
            return

        links = []
        for sym in sorted(symbols):
            base_sym = sym[:-4] if sym.endswith("USDT") else sym
            chart_url = f"{public_url}/chart?symbol={sym}"
            links.append(f"<a href='{chart_url}'><b>{base_sym}</b></a>")

        await reply(update, 
            f"📈 <b>Графики торгов за 24 часа</b>\n\n"
            f"{', '.join(links)}\n\n"
            f"<i>Кликните на монету для загрузки графика (2-3 сек)</i>"
        )
        return
        
    sym = arg.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
        
    chart_url = f"{public_url}/chart?symbol={sym}"
    
    await reply(update, 
        f"📈 <b>График торгов {sym}</b>\n\n"
        f"Скрипт распарсит логи и наложит их на свечи Bybit.\n\n"
        f"🌐 <a href='{chart_url}'>Открыть интерактивный график</a>\n"
        f"<i>(Генерация страницы займет 2-3 секунды)</i>"
    )

@restricted
async def cmd_autotune(update, context):
    arg = (context.args or [None])[0]
    if arg in ("on", "вкл"):
        shadow.set_auto(True)
    elif arg in ("off", "выкл"):
        shadow.set_auto(False)
    else:
        shadow.set_auto(not shadow.tuning["auto"])
    await reply(update, shadow.stats_text() +
                "\n💡 переключение: /autotune · или /autotune off · /autotune on")


@restricted
async def cmd_status(update, context):
    try:
        prices = await market_data.get_tickers()
        eq = paper.equity(prices)
        free_pct = paper.usdt / eq * 100 if eq else 0
        
        metrics_all = paper.get_metrics(prices)
        metrics_24h = paper.get_metrics(prices, hours=24)

        msg = ["📊 <b>Капитан Рост</b> · <i>тренировка</i> 🎓", ""]
        msg.append(f"💰 Свободно: <b>{usd(paper.usdt)}</b> ({free_pct:.0f}%)")
        msg.append(f"🏦 Накопления: <b>{usd(paper.funding)}</b>")
        msg.append(f"📈 Капитал: <b>{usd(eq)}</b>")
        msg.append(f"💵 PnL (за всё время): {pnl_emoji(metrics_all['total_pnl'])} <b>{usd(metrics_all['total_pnl'])}</b>")
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
                val = pos["qty"] * last
                w = val / eq * 100 if eq else 0
                pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
                
                ind = "🔥" if pos.get("tp1_done") else pnl_emoji(pnl_pct)
                tp1 = " · TP1" if pos.get("tp1_done") else ""
                
                # ИСПОЛЬЗУЕМ НОВЫЙ ФОРМАТИРОВЩИК
                msg.append(f"{format_coin(sym, pos)} · {ind} {fmt_pct(pnl_pct)}{tp1}")
                msg.append(f"   💼 {usd(val)} · {w:.1f}%")
                msg.append(f"   📥 {fmt_price(pos['avg'])} → 📊 {fmt_price(last)}")
                tp_pct = (pos["tp"] - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
                sl_pct = (pos["sl"] - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
                
                if sl_pct >= 0.5:
                    sl_str = f"📈 <b>{fmt_price(pos['sl'])} ({fmt_pct(sl_pct)})</b>"
                elif sl_pct >= 0.15: 
                    sl_str = f"🔒 <b>{fmt_price(pos['sl'])} ({fmt_pct(sl_pct)})</b>"
                else:
                    sl_str = f"🛡 {fmt_price(pos['sl'])} ({fmt_pct(sl_pct)})"
                    
                msg.append(f"   🎯 {fmt_price(pos['tp'])} ({fmt_pct(tp_pct)}) · {sl_str}")
        else:
            msg.append("📦 <b>Позиции</b>: нет")
        msg.append("")

        if paper.orders:
            orders_sum = sum(o["qty"] * o["price"] for o in paper.orders)
            msg.append(f"📋 <b>Ордера ({len(paper.orders)})</b> · {usd(orders_sum)}")
            for o in paper.orders:
                val = o["qty"] * o["price"]
                w = val / eq * 100 if eq else 0
                
                last_price = prices.get(o["symbol"], {}).get("last", 0)
                if last_price > 0:
                    dist_pct = (o["price"] - last_price) / last_price * 100
                    dist_str = f" ({dist_pct:+.2f}%)"
                else:
                    dist_str = ""
                
                # ИСПОЛЬЗУЕМ НОВЫЙ ФОРМАТИРОВЩИК
                msg.append(f"{format_coin(o['symbol'], o)} · {w:.1f}%")
                msg.append(f"   💼 {usd(val)} · 📥 {fmt_price(o['price'])}{dist_str}")
                tp_pct = (o["tp"] - o["price"]) / o["price"] * 100 if o["price"] else 0
                sl_pct = (o["sl"] - o["price"]) / o["price"] * 100 if o["price"] else 0
                msg.append(f"   🎯 {fmt_price(o['tp'])} ({fmt_pct(tp_pct)}) · 🛡 {fmt_price(o['sl'])} ({fmt_pct(sl_pct)})")
        else:
            msg.append("📋 <b>Ордера</b>: нет")
        msg.append("")

        mode, _ = learner.risk_mode(
            metrics_24h["profit_factor"], metrics_24h["max_drawdown_pct"], metrics_24h["total_trades"]
        )
        mode_emoji = {"NORMAL": "🟢", "CAUTIOUS": "🟡", "STRICT": "🔴", "AGGRESSIVE": "🚀"}.get(mode, "⚪")

        msg.append(f"📊 <b>Метрики (24ч)</b> · {mode_emoji} {mode}")
        msg.append(
            f"🧾 {metrics_24h['total_trades']} позиций "
            f"(✅ {metrics_24h['win_count']} / ❌ {metrics_24h['loss_count']}) · "
            f"🎯 частичных TP1: {metrics_24h['partial_count']}"
        )

        pf = metrics_24h["profit_factor"]
        if pf is None:
            pf_text, pf_mark = "—", ""
        elif pf == float("inf"):
            pf_text, pf_mark = "∞", "🎯"
        else:
            pf_text = f"{pf:.2f}"
            pf_mark = "🎯" if pf >= 1.3 else ("⚠️" if pf >= 1.0 else "❌")
        dd = metrics_24h["max_drawdown_pct"]
        dd_mark = "✅" if dd < 5 else ("⚠️" if dd < 15 else "🔴")
        
        if pf is None:
            msg.append(f"📈 PF: <b>—</b> · 📉 DD: <b>{dd:.1f}%</b> {dd_mark} (лимит 15%)")
        else:
            msg.append(f"📈 PF: <b>{pf_text}</b> {pf_mark} (цель ≥ 1.3) · 📉 DD: <b>{dd:.1f}%</b> {dd_mark} (лимит 15%)")

        exp = metrics_24h["expectancy"]
        if exp is None:
            exp_txt, exp_mark = "—", "⚪"
            exp_emoji = "⚪"
        else:
            exp_txt = f"{exp:+.2f}"
            exp_mark = "🎯" if exp > 0 else "❌"
            exp_emoji = pnl_emoji(exp)

        rf = metrics_24h["recovery_factor"]
        if rf is None:
            rf_txt, rf_mark = "—", "⚪"
        elif rf == float("inf"):
            rf_txt, rf_mark = "∞", "🎯"
        else:
            rf_txt = f"{rf:.1f}"
            rf_mark = "🎯" if rf >= 2 else ("⚠️" if rf >= 1 else "❌")

        if exp is None:
            msg.append(f"💹 <b>—</b> · 🔄 RF: <b>—</b>")
        else:
            msg.append(f"💹 {exp_emoji} <b>{exp_txt}</b> {exp_mark} (цель > 0) · 🔄 RF: <b>{rf_txt}</b> {rf_mark} (цель ≥ 2)")

        sat_exposure = sum(
            p["qty"] * prices.get(s, {}).get("last", 0)
            for s, p in paper.positions.items() if p.get("kind") == "satellite"
        ) + sum(
            o["qty"] * o["price"] for o in paper.orders if o.get("kind") == "satellite"
        )
        sat_pct = sat_exposure / eq * 100 if eq else 0
        msg.append(f"🛰 Сателлиты: <b>{sat_pct:.1f}%</b> / {learner.satellite_limit():.0f}%")
        msg.append(f"⏱ PnL за 24 часа: {pnl_emoji(metrics_24h['total_pnl'])} <b>{usd(metrics_24h['total_pnl'])}</b>")
        msg.append("")

        regime, _ = await get_regime()
        regime_emoji = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}.get(regime, "⚪")
        regime_text = {"bull": "BULL", "neutral": "NEUTRAL", "bear": "BEAR"}.get(regime, regime)

        from bot.strategy.fundamental import get_fear_and_greed
        fng = get_fear_and_greed()
        fng_emoji = "🤑" if fng >= 60 else ("😱" if fng <= 40 else "😴")

        btc = prices.get("BTCUSDT", {}).get("last", 0)
        msg.append(f"₿ <b>${fmt_price(btc)}</b> · {regime_emoji} {regime_text} · 🧭 F&G: {fng} {fng_emoji} · 🎯 порог {threshold(regime):g}")
        if SCAN_SUMMARY.get("text"):
            msg.append(f"🔎 {SCAN_SUMMARY['text']}")
        wr, n = learner.winrate()
        top = sorted(learner.weights.items(), key=lambda kv: kv[1], reverse=True)[:3]
        top_txt = " · ".join(f"{k} {v:.2f}" for k, v in top)
        msg.append(f"🧠 wr {wr:.0%} ({n}) · топ: {top_txt} · строгость {learner.threshold_adj:+.1f}")

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
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("learn", cmd_learn))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("exitall", cmd_exitall))
    app.add_handler(CommandHandler("resetstats", cmd_resetstats))
    app.add_handler(CommandHandler("resetlearn", cmd_resetlearn))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("autotune", cmd_autotune))
    app.add_handler(CallbackQueryHandler(confirm_handler, pattern="^(confirm:|cancel)"))

    logger.info("Бот собран, запускаем webhook-сервер...")
    asyncio.run(run_all(app))

if __name__ == '__main__':
    main()
