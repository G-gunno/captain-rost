import os
import asyncio
import calendar
import html as _html
import gzip
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from functools import wraps

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
from bot.strategy.scanner import SCAN_SUMMARY, FILTERED_BY_NEWS, get_regime, get_thresholds
from bot.strategy.learner import learner, is_neutral_be
from bot.news.cmc import memory_stats
from bot.utils.format import (
    format_coin, sl_emoji, grade_badge, mode_emoji,
    usd, pnl_emoji, weight_emoji, fmt_price, fmt_pct, fmt_sym
)

_app = None
WEBHOOK_PATH = "/telegram-webhook"


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


async def health_handler(request):
    return web.Response(status=204)


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
    interval_str = request.query.get("interval", "60")

    if not symbol:
        return web.Response(text="Укажите тикер, например ?symbol=LINKUSDT", status=400)
    symbol = symbol.upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    valid_intervals = ["1", "3", "5", "15", "30", "60", "120", "240", "D", "W"]
    if interval_str not in valid_intervals:
        interval_str = "60"

    def make_chart():
        try:
            from bot.utils.visualizer import TradeVisualizer
            viz = TradeVisualizer(log_path="logs/bot.log", symbol=symbol, interval=interval_str)
            fig = viz.build_chart(show=False)
            if fig is None:
                return None

            raw_html = fig.to_html(include_plotlyjs="cdn", full_html=True)

            buttons_html = (
                '<div style="position: absolute; top: 15px; left: 50%; transform: translateX(-50%); z-index: 1000; '
                'background: rgba(30, 30, 30, 0.85); padding: 8px 15px; border-radius: 8px; '
                'border: 1px solid #444; font-family: Arial, sans-serif; '
                'box-shadow: 0 4px 6px rgba(0,0,0,0.3); display: flex; align-items: center;">'
                '<span style="color: #ccc; margin-right: 12px; font-size: 14px;">Таймфрейм:</span>'
            )

            for tf in valid_intervals:
                bg_color = "#2962ff" if tf == interval_str else "#444"
                text_color = "#fff" if tf == interval_str else "#ccc"
                hover_style = "this.style.background='#555'" if tf != interval_str else ""
                out_style = f"this.style.background='{bg_color}'"

                buttons_html += (
                    f'<a href="?symbol={symbol}&interval={tf}" '
                    f'style="text-decoration: none; color: {text_color}; background: {bg_color}; '
                    f'padding: 5px 10px; margin: 0 3px; border-radius: 4px; font-size: 13px; font-weight: bold; transition: 0.2s;" '
                    f'onmouseover="{hover_style}" onmouseout="{out_style}">{tf}</a>'
                )

            buttons_html += "</div>"
            return raw_html.replace("<body>", f"<body style='margin:0; padding:0; background-color:#111;'>\n{buttons_html}")

        except Exception as e:
            return str(e)

    try:
        html_or_error = await asyncio.to_thread(make_chart)
        if html_or_error is None:
            return web.Response(text=f"Нет данных лога или свечей для {symbol}.", status=404)
        if not html_or_error.startswith("<"):
            return web.Response(text=f"Ошибка генерации: {html_or_error}", status=500)

        compressed_html = gzip.compress(html_or_error.encode("utf-8"))

        return web.Response(
            body=compressed_html,
            content_type="text/html",
            headers={
                "Content-Encoding": "gzip",
                "Vary": "Accept-Encoding"
            }
        )
    except Exception as e:
        return web.Response(text=f"Внутренняя ошибка сервера: {e}", status=500)


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
    if isinstance(err, TelegramConflict):
        return
    logger.exception(f"Unhandled error: {err}")


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
    return f"▶ <b>Возобновлено</b>: ордеров восстановлено {len(orders)}."


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
    return "🧠♻️ <b>Опыт ИИ сброшен</b>\n• Веса возвращены к 1.0\n• Журнал автотюна очищен."


async def action_resetstats(context):
    paper.reset_stats()
    learner.reset_stats()
    return "📊 <b>Статистика сброшена</b>\n💰 Баланс: <b>$1,000.00</b>\n📦 Ордера и позиции очищены."


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
                await query.edit_message_text(f"✅ <b>Подтверждено</b>\n\n{result}", reply_markup=InlineKeyboardMarkup([]), parse_mode="HTML")
            except BadRequest:
                await query.edit_message_text(f"✅ Подтверждено\n\n{result}", reply_markup=InlineKeyboardMarkup([]), parse_mode="HTML")
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
            try:
                await query.edit_message_text(f"⚠️ Ошибка: {e}", reply_markup=InlineKeyboardMarkup([]))
            except BadRequest:
                pass
        except Exception as e:
            logger.exception(f"confirm action error: {e}")


@restricted
async def cmd_start(update, context):
    bot_state.fresh_start()
    await reply(update, "🤖 <b>Капитан Рост</b> на связи! Торговля запущена.")


@restricted
async def cmd_info(update, context):
    from bot.services.info import info_full_text, generate_whitepaper
    import io
    await reply(update, info_full_text())
    whitepaper_text = generate_whitepaper()
    doc = io.BytesIO(whitepaper_text.encode("utf-8"))
    doc.name = "CaptainRost_Whitepaper.txt"
    await update.message.reply_document(document=doc, caption="📄 <b>Подробная документация (Whitepaper)</b>", parse_mode="HTML")


@restricted
async def cmd_help(update, context):
    await reply(update, "📖 <b>Мои команды</b>\n/status — метрики\n/info — инфо\n/learn — обучение\n/news — аналитика\n/pause, /resume, /exitall\n/log — лог")


@restricted
async def cmd_pause(update, context):
    await ask_confirmation(update, context, "pause")


@restricted
async def cmd_resume(update, context):
    await ask_confirmation(update, context, "resume")


@restricted
async def cmd_exitall(update, context):
    await ask_confirmation(update, context, "exitall")


@restricted
async def cmd_resetlearn(update, context):
    await ask_confirmation(update, context, "resetlearn")


@restricted
async def cmd_resetstats(update, context):
    await ask_confirmation(update, context, "resetstats")


@restricted
async def cmd_learn(update, context):
    wr, n = learner.winrate()
    a_stats = learner.get_grade_a_stats()
    lines = ["🧠 <b>Обучение бота (ИИ)</b>", ""]
    lines.append("📌 <b>Текущие параметры</b>")
    lines.append(
        f"🎯 Чистый Winrate: <b>{wr:.0%}</b> <i>(за {n} сдел.)</i> · "
        f"🛡🔒 БУ: <b>{learner.be_count}</b> · строгость: <b>{learner.threshold_adj:+.2f}</b>"
    )
    lines.append(
        f"💎 <b>Сетапы [💎]:</b> WR <b>{a_stats['wr']:.0%}</b> ({a_stats['decisive']} рез. + {a_stats['be']} 🛡🔒) · "
        f"ср. {a_stats['avg']:+.2f}% · серия {a_stats['streak']:+d} · вес <b>×{a_stats['mult']:.2f}</b>"
    )

    lines.append("\n🧭 <b>Где деньги</b>")
    if learner.sector_stats:
        rows = []
        for s, h in learner.sector_stats.items():
            dec = [p for p in h if not is_neutral_be(p)]
            if dec:
                rows.append((s, sum(1 for p in dec if p > 0) / len(dec), len(dec), sum(dec) / len(dec), learner.sector_bias(s)))
        rows.sort(key=lambda r: r[4], reverse=True)
        for s, swr, cnt, avg, bias in rows:
            lines.append(f"   {pnl_emoji(avg)} <i>{s}</i> · wr {swr:.0%} ({cnt}) · {avg:+.2f}% → {bias:+.2f}")

    lines.append("\n🎯 <b>Веса индикаторов контекста</b>")
    for k, v in sorted(learner.weights.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"   {weight_emoji(v)} <i>{k}</i> · {v:.2f} {'⚡' * max(1, int(round(v * 5)))}")

    lines.append("\n🧩 <b>Паттерны Price Action</b>")
    pat_names = {
        "pinbar": "🔨 Пин-бар выкупа (Spring)",
        "divergence": "🧲 Дивергенция RSI",
        "vcp": "📦 Сжатие волатильности (VCP)",
        "breakout": "🚀 Импульсный пробой",
        "engulfing": "⚡ Бычье поглощение"
    }
    if learner.pattern_stats:
        for p, h in sorted(learner.pattern_stats.items()):
            dec = [x for x in h if not is_neutral_be(x)]
            if dec:
                pwr = sum(1 for x in dec if x > 0) / len(dec)
                pavg = sum(dec) / len(dec)
                pmult = learner.pattern_multiplier(p)
                lines.append(f"   {pnl_emoji(pavg)} {pat_names.get(p, p)}: {len(dec)} сдел. · wr {pwr:.0%} · ср. {pavg:+.2f}% (×{pmult:.2f})")
    else:
        lines.append("   <i>(накапливается статистика по сетапам)</i>")

    lines.append("\n🧾 <b>Как выходим</b>")
    if learner.exit_stats:
        rows = sorted(
            [(t, sum(1 for p in h if p > 0) / len(h), len(h), sum(h) / len(h)) for t, h in learner.exit_stats.items() if h],
            key=lambda r: r[3],
            reverse=True
        )
        for t, twr, cnt, avg in rows:
            lines.append(f"   {pnl_emoji(avg)} {t} · {cnt} сдел. · wr {twr:.0%} · {avg:+.2f}%")

    mem = memory_stats()
    lines.append(f"\n🗂 <b>Память:</b> 📚 База {mem['base']} + выучено {mem['learned']} = <b>{mem['total']}</b>")
    lines.extend(shadow.learn_lines())
    await reply(update, "\n".join(lines))


@restricted
async def cmd_funding(update, context):
    try:
        prices = await market_data.get_tickers()
        coins = getattr(paper, "funding_coins", {})
        usdt_fund = paper.funding

        lines = ["🏦 <b>Накопительный фонд (Funding Vault)</b>", ""]
        lines.append(f"💵 <b>Стейблкоины (USDT):</b> <b>{usd(usdt_fund)}</b>")
        lines.append("<i>Резерв 30% с профита альткоинов. Доступен для ручного вывода или экстренных нужд.</i>\n")

        active_coins = {s: q for s, q in coins.items() if q > 0.000001}
        if active_coins:
            coins_val = sum(qty * prices.get(s, {}).get("last", 0) for s, qty in active_coins.items())
            lines.append(f"🐋 <b>HODL-портфель китов (TOP20): ~{usd(coins_val)}</b>")
            for sym, qty in sorted(active_coins.items(), key=lambda kv: kv[1] * prices.get(kv[0], {}).get("last", 0), reverse=True):
                last = prices.get(sym, {}).get("last", 0)
                val = qty * last
                coin_name = sym[:-4] if sym.endswith("USDT") else sym
                lines.append(f"• <b>{coin_name}</b>: {qty:.6f} (~{usd(val)}) · по ${fmt_price(last)}")
            lines.append("\n<i>Эти монеты не участвуют в торговле и накапливаются как чистый крипто-актив.</i>")
        else:
            lines.append("🐋 <b>HODL-портфель китов:</b> монет пока нет")

        await reply(update, "\n".join(lines))
    except Exception as e:
        logger.exception("Ошибка в /funding")
        await reply(update, f"⚠️ Ошибка: {e}")


@restricted
async def cmd_news(update, context):
    from bot.news.cmc import get_stats as cmc_stats
    from bot.news.rss_news import get_stats as rss_stats
    cmc, rss = cmc_stats(), rss_stats()
    lines = ["📰 <b>Новостная аналитика</b>\n\n📡 <b>RSS-ленты</b>"]
    if rss["feeds_working"]:
        lines.append(f"   ✅ работают · {rss['items_count']} новостей · ⏱ {rss['cache_age_min']} мин назад")
        if rss["neg_examples"]:
            lines.append(f"   ⚠️ негатив: {_html.escape(rss['neg_examples'][0][:60])}…")
    else:
        lines.append("   ❌ ленты недоступны")
    lines.append(f"\n🏷 <b>CoinMarketCap:</b> {'✅' if cmc['api_key_set'] else '❌'} API · 📚 {cmc['sectors_learned']} сек · 🏆 {cmc['ranks_cached']} рангов")
    if FILTERED_BY_NEWS:
        lines.append("\n🚫 <b>Отфильтровано новостями:</b>")
        for i in FILTERED_BY_NEWS[-5:]:
            lines.append(f"   • {_html.escape(fmt_sym(i['symbol']))} · {i['neg_count']}")
    await reply(update, "\n".join(lines))


@restricted
async def cmd_log(update, context):
    src = Path("logs/bot.log")
    if not src.exists():
        return await reply(update, "⚠️ Файл лога не найден.")
    name = f"log_{datetime.now().strftime('%H%M%S')}.txt"
    Path("logs", name).write_bytes(src.read_bytes())
    with open(Path("logs", name), "rb") as f:
        await update.message.reply_document(document=f, filename=name)


@restricted
async def cmd_chart(update, context):
    import time
    import io
    from bot.services.reports import build_trades_audit_24h

    arg = (context.args or [None])[0]
    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")

    if not arg:
        cutoff = int(time.time()) - 86400
        active_syms = {sym: p for sym, p in paper.positions.items()}
        for o in paper.orders:
            if o["symbol"] not in active_syms:
                active_syms[o["symbol"]] = o

        closed_trades = [t for t in paper.realized if not t.get("partial") and t.get("time", 0) >= cutoff]

        if not active_syms and not closed_trades:
            return await reply(update, "⚠️ За последние 24 часа активности не было.")

        lines = ["📈 <b>Графики торгов (24ч)</b>\n"]

        if active_syms:
            curr_reg = bot_state.current_regime.upper()
            curr_icon = "🟢" if curr_reg == "BULL" else "🔴" if curr_reg == "BEAR" else "🟡"
            lines.append(f"📦 <b>В работе ({curr_icon} {curr_reg}):</b>")
            links = []
            for sym, data in sorted(active_syms.items()):
                m_icon = mode_emoji(data)
                links.append(f"{m_icon} <a href='{public_url}/chart?symbol={sym}'><b>{sym[:-4]}</b></a>{grade_badge(data)}")
            lines.append(" · ".join(links) + "\n")

        regimes_order = [
            ("bull", "🟢 <b>Бычий рынок (BULL)</b>"),
            ("neutral", "🟡 <b>Нейтральный рынок (NEUTRAL)</b>"),
            ("bear", "🔴 <b>Медвежий рынок (BEAR)</b>"),
            ("unknown", "⚪ <b>Архивные сделки</b>")
        ]

        for reg_key, reg_title in regimes_order:
            trades_in_reg = [t for t in closed_trades if t.get("regime", "unknown") == reg_key]
            if not trades_in_reg:
                continue

            lines.append(reg_title)

            be_list = [t for t in trades_in_reg if t.get("is_be") or is_neutral_be(t.get("pnl_pct", 0.0))]
            decisive_list = [t for t in trades_in_reg if t not in be_list]
            wins = [t for t in decisive_list if t["pnl"] > 0]
            losses = [t for t in decisive_list if t["pnl"] <= 0]

            if wins:
                win_links = []
                for t in sorted(wins, key=lambda x: x["pnl_pct"], reverse=True):
                    sym = t["symbol"][:-4]
                    win_links.append(f"{mode_emoji(t)} <a href='{public_url}/chart?symbol={t['symbol']}'><b>{sym}</b></a>{grade_badge(t)} ({t['pnl_pct']:+.1f}%)")
                lines.append("✅ " + " · ".join(win_links))

            if be_list:
                be_links = []
                for t in be_list:
                    sym = t["symbol"][:-4]
                    be_links.append(f"{mode_emoji(t)} <a href='{public_url}/chart?symbol={t['symbol']}'><b>{sym}</b></a> ({t['pnl_pct']:+.2f}%)")
                lines.append("🛡🔒 " + " · ".join(be_links))

            if losses:
                loss_links = []
                for t in sorted(losses, key=lambda x: x["pnl_pct"]):
                    sym = t["symbol"][:-4]
                    loss_links.append(f"{mode_emoji(t)} <a href='{public_url}/chart?symbol={t['symbol']}'><b>{sym}</b></a>{grade_badge(t)} ({t['pnl_pct']:+.1f}%)")
                lines.append("❌ " + " · ".join(loss_links))

            lines.append("")

        await reply(update, "\n".join(lines).strip())

        try:
            tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
            ts_str = datetime.now(tz).strftime("%H%M%S")
            audit_text = await build_trades_audit_24h(tz)
            audit_doc = io.BytesIO(audit_text.encode("utf-8"))
            audit_name = f"audit_24h_{ts_str}.txt"
            audit_doc.name = audit_name
            await update.message.reply_document(
                document=audit_doc,
                filename=audit_name,
                caption="📊 <b>Полный табличный аудит сделок и монет (24ч)</b>",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception(f"audit_24h build error: {e}")
        return

    sym = arg.upper() + ("USDT" if not arg.upper().endswith("USDT") else "")
    await reply(update, f"📈 <b>График торгов {sym}</b>\n\n🌐 <a href='{public_url}/chart?symbol={sym}'>Открыть интерактивный график</a>")


@restricted
async def cmd_autotune(update, context):
    arg = (context.args or [None])[0]
    shadow.set_auto(True if arg in ("on", "вкл") else False if arg in ("off", "выкл") else not shadow.tuning["auto"])
    await reply(update, shadow.stats_text() + "\n💡 переключение: /autotune on|off")


@restricted
async def cmd_status(update, context):
    try:
        prices = await market_data.get_tickers()
        eq = paper.equity(prices)

        locked_usdt = sum(o["qty"] * o["price"] for o in paper.orders)
        available_usdt = max(0.0, paper.usdt - locked_usdt)
        free_pct = available_usdt / eq * 100 if eq else 0

        metrics_all = paper.get_metrics(prices)
        metrics_24h = paper.get_metrics(prices, hours=24)

        msg = ["📊 <b>Капитан Рост</b> · <i>тренировка</i> 🎓", ""]
        msg.append(f"💰 Доступно: <b>{usd(available_usdt)}</b> ({free_pct:.0f}%)")
        if locked_usdt > 0:
            msg.append(f"🔒 В ордерах: <b>{usd(locked_usdt)}</b>")
        msg.append(f"🏦 Накопления USDT: <b>{usd(paper.funding)}</b>")

        active_coins = {s: q for s, q in getattr(paper, "funding_coins", {}).items() if q > 0.000001}
        if active_coins:
            coins_val = sum(q * prices.get(s, {}).get("last", 0) for s, q in active_coins.items())
            msg.append(f"🐋 HODL (монеты): <b>~{usd(coins_val)}</b> ({len(active_coins)} шт · /funding)")

        msg.append(f"📈 Торговый капитал: <b>{usd(eq)}</b>")
        msg.append(f"💵 PnL (за всё время): {pnl_emoji(metrics_all['total_pnl'])} <b>{usd(metrics_all['total_pnl'])}</b>")
        msg.append("")

        if paper.positions:
            inv_pct = sum(p["qty"] * prices.get(s, {}).get("last", 0) for s, p in paper.positions.items()) / eq * 100 if eq else 0
            msg.append(f"📦 <b>Позиции ({len(paper.positions)})</b> · {inv_pct:.0f}% портфеля")
            for sym, pos in paper.positions.items():
                last = prices.get(sym, {}).get("last", 0)
                val, w = pos["qty"] * last, (pos["qty"] * last / eq * 100 if eq else 0)
                pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0

                tp1_status = " · 🔥 TP1" if pos.get("tp1_done") else ""
                msg.append(f"{format_coin(sym, pos)}{grade_badge(pos)} · {pnl_emoji(pnl_pct)} <b>{fmt_pct(pnl_pct)}</b>{tp1_status}")
                msg.append(f"   💼 {usd(val)} · {w:.1f}%")
                msg.append(f"   📥 {fmt_price(pos['avg'])} → 📊 {fmt_price(last)}")

                tp_pct = (pos["tp"] - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
                sl_pct = (pos["sl"] - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0

                tp_icon = "TP🏃🏻‍➡️" if pos.get("tp1_done") else "🎯"
                sl_tag = sl_emoji(pos.get("sl_state", "SHIELD"))
                msg.append(f"   {tp_icon} {fmt_price(pos['tp'])} ({fmt_pct(tp_pct)}) · {sl_tag} <b>{fmt_price(pos['sl'])} ({fmt_pct(sl_pct)})</b>")
        else:
            msg.append("📦 <b>Позиции</b>: нет")
        msg.append("")

        if paper.orders:
            msg.append(f"📋 <b>Ордера ({len(paper.orders)})</b> · {usd(sum(o['qty'] * o['price'] for o in paper.orders))}")
            for o in paper.orders:
                val, w = o["qty"] * o["price"], (o["qty"] * o["price"] / eq * 100 if eq else 0)
                last_price = prices.get(o["symbol"], {}).get("last", 0)
                dist_str = f" ({(o['price'] - last_price) / last_price * 100:+.2f}%)" if last_price > 0 else ""

                msg.append(f"{format_coin(o['symbol'], o)}{grade_badge(o)} · {w:.1f}%")
                msg.append(f"   💼 {usd(val)} · 📥 {fmt_price(o['price'])}{dist_str}")
                msg.append(
                    f"   🎯 {fmt_price(o['tp'])} ({fmt_pct((o['tp'] - o['price']) / o['price'] * 100 if o['price'] else 0)}) · "
                    f"🛡📉 {fmt_price(o['sl'])} ({fmt_pct((o['sl'] - o['price']) / o['price'] * 100 if o['price'] else 0)})"
                )
        else:
            msg.append("📋 <b>Ордера</b>: нет")
        msg.append("")

        mode, _ = learner.risk_mode(metrics_24h["profit_factor"], metrics_24h["max_drawdown_pct"], metrics_24h["total_trades"])
        mode_emoji_map = {"NORMAL": "🟢", "CAUTIOUS": "🟡", "STRICT": "🔴", "AGGRESSIVE": "🚀"}
        m_em = mode_emoji_map.get(mode, "⚪")

        msg.append(f"📊 <b>Метрики (24ч)</b> · {m_em} {mode}")
        msg.append(
            f"🧾 {metrics_24h['total_trades']} (✅ {metrics_24h['win_count']} / ❌ {metrics_24h['loss_count']} / 🛡🔒 {metrics_24h['be_count']}) · "
            f"🔥TP1: {metrics_24h['partial_count']}"
        )

        a_stats = learner.get_grade_a_stats()
        if a_stats["total"] > 0:
            msg.append(
                f"💎 [💎] A+: WR <b>{a_stats['wr']:.0%}</b> ({a_stats['decisive']}) · "
                f"вес <b>×{a_stats['mult']:.2f}</b> (серия {a_stats['streak']:+d})"
            )

        pf = metrics_24h["profit_factor"]
        pf_text, pf_mark = ("—", "") if pf is None else ("∞", "🎯") if pf == float("inf") else (f"{pf:.2f}", "🎯" if pf >= 1.3 else ("⚠️" if pf >= 1.0 else "❌"))
        dd = metrics_24h["max_drawdown_pct"]
        dd_mark = "✅" if dd < 5 else ("⚠️" if dd < 15 else "🔴")

        if pf is None:
            msg.append(f"📈 PF: <b>—</b> · 📉 DD: <b>{dd:.1f}%</b> {dd_mark} (лимит 15%)")
        else:
            msg.append(f"📈 PF: <b>{pf_text}</b> {pf_mark} (цель ≥ 1.3) · 📉 DD: <b>{dd:.1f}%</b> {dd_mark} (лимит 15%)")

        exp = metrics_24h["expectancy"]
        rf = metrics_24h["recovery_factor"]
        exp_str = "—" if exp is None else f"{pnl_emoji(exp)} <b>{exp:+.2f}</b> {'🎯' if exp > 0 else '❌'}"
        rf_str = "—" if rf is None else "∞" if rf == float("inf") else f"{rf:.1f} {'🎯' if rf >= 2 else ('⚠️' if rf >= 1 else '❌')}"
        msg.append(f"💹 {exp_str} · 🔄 RF: <b>{rf_str}</b>")
        msg.append(f"⏱ PnL за 24 часа: {pnl_emoji(metrics_24h['total_pnl'])} <b>{usd(metrics_24h['total_pnl'])}</b>\n")

        regime = bot_state.current_regime
        thrs = SCAN_SUMMARY.get("thrs") or get_thresholds(regime)

        from bot.strategy.fundamental import get_fear_and_greed
        fng = get_fear_and_greed()
        fng_emoji = "🌋" if fng >= 75 else "🤑" if fng >= 55 else "😱" if fng <= 24 else "😨" if fng <= 45 else "😴"

        regime_str = {"bull": "🟢 BULL", "neutral": "🟡 NEUTRAL", "bear": "🔴 BEAR"}.get(regime, "⚪")
        btc_price = prices.get("BTCUSDT", {}).get("last", 0)

        msg.append(f"₿ <b>${fmt_price(btc_price)}</b> · {regime_str} · {fng_emoji} F&G: {fng}")
        msg.append(f"🎯 Пороги: 🚀 <b>{thrs['rocket']:.2f}</b> | 🏹 <b>{thrs['sniper']:.2f}</b> | 🧲 <b>{thrs['reversal']:.2f}</b>")

        if SCAN_SUMMARY.get("text"):
            msg.append(f"🔎 {SCAN_SUMMARY['text']}")
        wr, n = learner.winrate()
        top_txt = " · ".join(f"{k} {v:.2f}" for k, v in sorted(learner.weights.items(), key=lambda kv: kv[1], reverse=True)[:3])
        msg.append(f"🧠 wr {wr:.0%} ({n}) · 🛡🔒 {learner.be_count} · топ: {top_txt} · строгость {learner.threshold_adj:+.2f}")

        await reply(update, "\n".join(msg))
    except Exception as e:
        logger.exception("Ошибка в /status")
        await reply(update, f"⚠️ Ошибка: {e}")


async def run_all(application):
    global _app
    _app = application
    await application.initialize()
    await application.start()

    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")
    webhook_url = f"{public_url}{WEBHOOK_PATH}"

    await application.bot.set_webhook(
        url=webhook_url, drop_pending_updates=True,
        allowed_updates=["message", "edited_message", "callback_query"],
    )

    await application.bot.set_my_commands([
        BotCommand("start", "🚀 Запустить торговлю"),
        BotCommand("pause", "⏸ Пауза"),
        BotCommand("resume", "▶️ Возобновить"),
        BotCommand("status", "📊 Статус"),
        BotCommand("funding", "🏦 Накопительный фонд"),
        BotCommand("chart", "📈 График"),
        BotCommand("learn", "🧠 Обучение"),
        BotCommand("news", "📰 Новости"),
        BotCommand("exitall", "🛑 Продать всё"),
        BotCommand("resetstats", "📊 Сброс статы"),
        BotCommand("resetlearn", "🧠♻️ Сброс ИИ"),
        BotCommand("log", "📄 Лог"),
        BotCommand("autotune", "🎛 Автотюн"),
        BotCommand("info", "📖 Инфо"),
        BotCommand("help", "📖 Справка"),
    ])

    await asyncio.to_thread(ensure_branch)

    from bot.core.event_bus import EventBus
    from bot.workers.market_data import MarketDataWorker
    from bot.workers.execution import ExecutionRiskWorker
    from bot.workers.scanner import ScannerWorker
    from bot.workers.order_manager import OrderManagerWorker
    from bot.workers.notification import NotificationWorker
    from bot.workers.position_manager import PositionManagerWorker
    from bot.workers.data_feeder import DataFeederWorker

    global_bus = EventBus()

    workers = [
        asyncio.create_task(MarketDataWorker(global_bus).run(), name="MarketData"),
        asyncio.create_task(ExecutionRiskWorker(global_bus).run(), name="Execution"),
        asyncio.create_task(ScannerWorker(global_bus).run(), name="Scanner"),
        asyncio.create_task(OrderManagerWorker(global_bus).run(), name="OrderManager"),
        asyncio.create_task(PositionManagerWorker(global_bus).run(), name="PositionManager"),
        asyncio.create_task(NotificationWorker(global_bus, send_chat_func=send_chat).run(), name="Notification"),
        asyncio.create_task(DataFeederWorker(global_bus).run(), name="DataFeeder"),
    ]

    def worker_callback(t: asyncio.Task):
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"Воркер {t.get_name()} упал: {e}")

    for task in workers:
        task.add_done_callback(worker_callback)

    asyncio.create_task(report_loop())

    logger.info("⚡ EDA Core успешно запущено. (WEBHOOK MODE)")

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
    app.add_handler(CommandHandler("funding", cmd_funding))
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

    asyncio.run(run_all(app))


if __name__ == "__main__":
    main()
