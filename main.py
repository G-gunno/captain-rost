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
from bot.core.state import bot_state
from bot.core.remote_state import ensure_branch
from bot.services.reports import build_report
from bot.services.info import info_full_text
from bot.strategy.shadow import shadow
from bot.strategy.scanner import SCAN_SUMMARY, FILTERED_BY_NEWS, get_regime, threshold
from bot.strategy.learner import learner, TIERS
from bot.news.cmc import TIER_EMOJI, TIER_NAMES, memory_stats
from bot.utils.format import format_coin, usd, pnl_emoji, weight_emoji, fmt_price, fmt_pct, fmt_sym

_app = None
WEBHOOK_PATH = "/telegram-webhook"

# ==================== Хелперы ====================
from functools import wraps

def restricted(func):
    @wraps(func)
    async def wrapped(update, context, *args, **kwargs):
        user_chat_id = str(update.effective_chat.id)
        admin_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if user_chat_id != admin_chat_id:
            logger.warning(f"🚨 Попытка взлома! Заблокирован доступ от чата: {user_chat_id}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

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
    if not symbol.endswith("USDT"): symbol += "USDT"

    def make_chart():
        try:
            from bot.utils.visualizer import TradeVisualizer
            viz = TradeVisualizer(log_path="logs/bot.log", symbol=symbol)
            fig = viz.build_chart(show=False) 
            if fig is None: return None
            return fig.to_html(include_plotlyjs="cdn", full_html=True)
        except Exception as e:
            return str(e)

    try:
        html_or_error = await asyncio.to_thread(make_chart)
        if html_or_error is None:
            return web.Response(text=f"Нет данных лога или свечей для {symbol}.", status=404)
        if not html_or_error.startswith("<"):
             return web.Response(text=f"Ошибка генерации: {html_or_error}", status=500)
        return web.Response(text=html_or_error, content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Внутренняя ошибка сервера: {e}", status=500)

# ==================== Уведомления и Отчеты ====================
async def send_chat(text):
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if chat and _app:
        try:
            await _app.bot.send_message(chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True)
        except BadRequest:
            await _app.bot.send_message(chat_id=chat, text=text, disable_web_page_preview=True)

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
    if isinstance(err, TelegramConflict): return
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
    except Exception: pass

    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")
    webhook_url = f"{public_url}{WEBHOOK_PATH}"

    await application.bot.set_webhook(
        url=webhook_url, drop_pending_updates=True,
        allowed_updates=["message", "edited_message", "callback_query"],
    )

    await application.bot.set_my_commands([
        BotCommand("status", "📊 Статус: балансы и позиции"),
        # ... твои команды
    ])

    await asyncio.to_thread(ensure_branch)
    
    # === ЗАПУСК НОВОЙ EVENT-DRIVEN АРХИТЕКТУРЫ ===
    from bot.core.event_bus import EventBus
    from bot.workers.market_data import MarketDataWorker
    from bot.workers.execution import ExecutionRiskWorker
    from bot.workers.scanner import ScannerWorker
    from bot.workers.order_manager import OrderManagerWorker
    from bot.workers.notification import NotificationWorker
    from bot.workers.position_manager import PositionManagerWorker

    global_bus = EventBus()
    
    workers = [
        asyncio.create_task(MarketDataWorker(global_bus).run(), name="Worker-MarketData"),
        asyncio.create_task(ExecutionRiskWorker(global_bus).run(), name="Worker-Execution"),
        asyncio.create_task(ScannerWorker(global_bus).run(), name="Worker-Scanner"),
        asyncio.create_task(OrderManagerWorker(global_bus).run(), name="Worker-OrderManager"),
        asyncio.create_task(PositionManagerWorker(global_bus).run(), name="Worker-PositionManager"),
        asyncio.create_task(NotificationWorker(global_bus, send_chat_func=send_chat).run(), name="Worker-Notification"),
    ]

    def worker_callback(t: asyncio.Task):
        try: t.result()
        except asyncio.CancelledError: pass
        except Exception as e: logger.exception(f"Воркер {t.get_name()} упал: {e}")

    for task in workers:
        task.add_done_callback(worker_callback)
    # ====================================================================

    asyncio.create_task(report_loop())
    
    from bot.strategy.fundamental import update_fundamental_data
    asyncio.create_task(update_fundamental_data())
    
    logger.info("EDA запущена. (WEBHOOK MODE)")

    web_app = web.Application()
    web_app.router.add_get("/", health_handler)
    web_app.router.add_get("/chart", chart_handler)
    web_app.router.add_post(WEBHOOK_PATH, webhook_handler)

    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.stop()
        await application.shutdown()
        await runner.cleanup()

def main():
    os.makedirs("logs", exist_ok=True)
    logger.add("logs/bot.log", rotation="5 MB", retention="7 days", enqueue=True, level="INFO")
    
    token = os.getenv("TELEGRAM_BOT_TOKEN")
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
