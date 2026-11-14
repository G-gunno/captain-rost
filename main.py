import os
import asyncio
import calendar
import html as _html
import gzip
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import aiohttp.web as web
from loguru import logger
from telegram import BotCommand
from telegram.error import TimedOut, NetworkError
from telegram.ext import Application

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.core.state import bot_state
from bot.core.remote_state import ensure_branch
from bot.services.reports import build_report, build_audit_html
from bot.services.info import info_full_text, generate_whitepaper
from bot.strategy.shadow import shadow
from bot.strategy.scanner import SCAN_SUMMARY, FILTERED_BY_NEWS, get_thresholds
from bot.strategy.learner import learner, is_neutral_be
from bot.news.cmc import memory_stats
from bot.utils.format import (
    format_coin, sl_emoji, grade_badge, mode_emoji,
    usd, pnl_emoji, weight_emoji, fmt_price, fmt_pct, fmt_sym
)

_app = None
WEBHOOK_PATH = "/telegram-webhook"


def _is_authorized(chat_id) -> bool:
    admin_chat_id = str(os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    if not admin_chat_id:
        return True
    if str(chat_id) != admin_chat_id:
        logger.warning(f"🚨 Попытка взлома! Заблокирован доступ от чата: {chat_id}")
        return False
    return True


async def health_handler(request):
    return web.Response(status=204)


async def log_download_handler(request):
    """Прямое скачивание файла лога через браузер."""
    src = Path("logs/bot.log")
    if not src.exists():
        return web.Response(text="Файл лога пока пуст.", status=404)
    name = f"log_{datetime.now().strftime('%H%M%S')}.txt"
    return web.Response(
        body=src.read_bytes(),
        content_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'}
    )


async def whitepaper_download_handler(request):
    """Прямое скачивание документации Whitepaper."""
    text = generate_whitepaper()
    return web.Response(
        body=text.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="CaptainRost_Whitepaper.txt"'}
    )


async def audit_handler(request):
    try:
        tz = ZoneInfo(os.getenv("TIMEZONE", "Europe/Moscow"))
        public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")
        try:
            hours = int(request.query.get("hours", 48))
        except (TypeError, ValueError):
            hours = 48
        html_page = await build_audit_html(tz, public_url=public_url, hours=hours)
        compressed_html = gzip.compress(html_page.encode("utf-8"))
        return web.Response(
            body=compressed_html,
            content_type="text/html",
            headers={
                "Content-Encoding": "gzip",
                "Vary": "Accept-Encoding"
            }
        )
    except Exception as e:
        logger.exception(f"audit_handler error: {e}")
        return web.Response(text=f"Ошибка генерации дашборда /audit: {e}", status=500)


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
                'box-shadow: 0 4px 6px rgba(0,0,0,0.3); display: flex; align-items: center; flex-wrap: wrap; gap: 4px;">'
                '<a href="/audit?hours=48" style="text-decoration: none; color: #00e5ff; background: rgba(0, 229, 255, 0.14); '
                'border: 1px solid rgba(0, 229, 255, 0.4); padding: 5px 10px; margin-right: 6px; border-radius: 4px; '
                'font-size: 13px; font-weight: bold;">📊 Аудит 48ч</a>'
                '<button id="chart-shot-btn" onclick="takeChartScreenshot()" '
                'style="cursor: pointer; color: #00e676; background: rgba(0, 230, 118, 0.16); '
                'border: 1px solid rgba(0, 230, 118, 0.45); padding: 5px 10px; margin-right: 10px; border-radius: 4px; '
                'font-size: 13px; font-weight: bold;">📸 Скриншот</button>'
                '<span style="color: #ccc; margin-right: 6px; font-size: 14px;">ТФ:</span>'
            )

            for tf in valid_intervals:
                bg_color = "#2962ff" if tf == interval_str else "#444"
                text_color = "#fff" if tf == interval_str else "#ccc"
                hover_style = "this.style.background='#555'" if tf != interval_str else ""
                out_style = f"this.style.background='{bg_color}'"

                buttons_html += (
                    f'<a href="?symbol={symbol}&interval={tf}" '
                    f'style="text-decoration: none; color: {text_color}; background: {bg_color}; '
                    f'padding: 5px 10px; margin: 0 2px; border-radius: 4px; font-size: 13px; font-weight: bold; transition: 0.2s;" '
                    f'onmouseover="{hover_style}" onmouseout="{out_style}">{tf}</a>'
                )

            buttons_html += "</div>"

            shot_script = f"""
            <script>
              async function takeChartScreenshot() {{
                const btn = document.getElementById('chart-shot-btn');
                const orig = btn.innerHTML;
                btn.innerHTML = '⏳ Снимок...';
                btn.disabled = true;
                try {{
                  const gd = document.querySelector('.js-plotly-plot');
                  const dataUrl = await Plotly.toImage(gd, {{
                    format: 'png',
                    width: 1600,
                    height: 950,
                    scale: 2
                  }});
                  const a = document.createElement('a');
                  const nowStr = new Date().toISOString().slice(0, 16).replace('T', '_').replace(':', '-');
                  a.href = dataUrl;
                  a.download = 'CaptainRost_{symbol}_{interval_str}_' + nowStr + '.png';
                  document.body.appendChild(a);
                  a.click();
                  a.remove();
                }} catch (e) {{
                  alert('Ошибка скриншота: ' + e);
                }} finally {{
                  btn.innerHTML = orig;
                  btn.disabled = false;
                }}
              }}
            </script>
            """

            patched = raw_html.replace(
                "<body>",
                f"<body style='margin:0; padding:0; background-color:#111;'>\n{buttons_html}"
            )
            return patched.replace("</body>", f"{shot_script}\n</body>")

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
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not (chat and token):
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.post(url, json={
                    "chat_id": chat,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                })
                if r.status_code == 200:
                    return
                await client.post(url, json={
                    "chat_id": chat,
                    "text": text,
                    "disable_web_page_preview": True,
                })
                return
        except Exception as e:
            if attempt == 0:
                await asyncio.sleep(1.0)
            else:
                logger.debug(f"Фоновое уведомление Telegram отложено ({type(e).__name__})")


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


async def action_pause(context=None):
    if bot_state.paused:
        return "⏸ Уже на паузе."
    orders = list(paper.orders)
    paper.orders = []
    paper.save()
    bot_state.pause(orders)
    return f"⏸ <b>Пауза</b>: ордеров снято {len(orders)}, позиции открыты."


async def action_resume(context=None):
    if not bot_state.paused:
        return "▶️ Не на паузе."
    orders = bot_state.resume()
    paper.orders.extend(orders)
    paper.save()
    return f"▶ <b>Возобновлено</b>: ордеров восстановлено {len(orders)}."


async def action_exitall(context=None):
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


async def action_resetlearn(context=None):
    learner.reset()
    shadow.reset()
    return "🧠♻️ <b>Опыт ИИ сброшен</b>\n• Веса возвращены к 1.0\n• Журнал автотюна очищен."


async def action_resetstats(context=None):
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


def _confirm_payload(key: str):
    _, question = ACTIONS[key]
    markup = {
        "inline_keyboard": [[
            {"text": "✅ Подтвердить", "callback_data": f"confirm:{key}"},
            {"text": "❌ Отмена", "callback_data": "cancel"},
        ]]
    }
    return f"⚠️ <b>Подтвердите:</b> {_html.escape(question)}", markup


async def _build_learn_text() -> str:
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
    return "\n".join(lines)


async def _build_funding_text() -> str:
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

    return "\n".join(lines)


async def _build_news_text() -> str:
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
    return "\n".join(lines)


async def _build_chart_text(args: list) -> str:
    import time
    arg = (args or [None])[0]
    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")

    if not arg:
        cutoff = int(time.time()) - 48 * 3600
        active_syms = {sym: p for sym, p in paper.positions.items()}
        for o in paper.orders:
            if o["symbol"] not in active_syms:
                active_syms[o["symbol"]] = o

        closed_trades = [t for t in paper.realized if not t.get("partial") and t.get("time", 0) >= cutoff]

        lines = [
            "📈 <b>Графики торгов и Аудит (48ч)</b>",
            f"🌐 <a href='{public_url}/audit?hours=48'><b>Открыть интерактивный дашборд инфографики (48ч)</b></a>\n"
        ]

        if not active_syms and not closed_trades:
            lines.append("<i>За последние 48 часов активности не было.</i>")
            return "\n".join(lines)

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

        return "\n".join(lines).strip()

    sym = arg.upper() + ("USDT" if not arg.upper().endswith("USDT") else "")
    return (
        f"📈 <b>График торгов {sym}</b>\n\n"
        f"🌐 <a href='{public_url}/chart?symbol={sym}'>Открыть интерактивный график</a>\n"
        f"📊 <a href='{public_url}/audit?hours=48'>Открыть общий дашборд аудита (48ч)</a>"
    )


async def _build_status_text() -> str:
    prices = await market_data.get_tickers()
    eq = paper.equity(prices)

    locked_usdt = paper.effective_locked_usdt()
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

    return "\n".join(msg)


async def _dispatch_command(cmd: str, args: list):
    """Выполняет команду и возвращает (text, reply_markup_dict) для мгновенного ответа в Webhook."""
    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")

    if cmd == "start":
        bot_state.fresh_start()
        return "🤖 <b>Капитан Рост</b> на связи! Торговля запущена.", None

    if cmd == "help":
        return (
            "📖 <b>Мои команды</b>\n"
            "/status — метрики\n"
            "/chart — графики и веб-аудит (48ч)\n"
            "/info — инфо\n"
            "/learn — обучение\n"
            "/news — аналитика\n"
            "/pause, /resume, /exitall\n"
            "/resetstats, /resetlearn\n"
            "/log — лог"
        ), None

    if cmd in ACTIONS:
        return _confirm_payload(cmd)

    if cmd == "status":
        return await _build_status_text(), None

    if cmd == "learn":
        return await _build_learn_text(), None

    if cmd == "funding":
        return await _build_funding_text(), None

    if cmd == "news":
        return await _build_news_text(), None

    if cmd == "chart":
        return await _build_chart_text(args), None

    if cmd == "autotune":
        arg = (args or [None])[0]
        shadow.set_auto(True if arg in ("on", "вкл") else False if arg in ("off", "выкл") else not shadow.tuning["auto"])
        return shadow.stats_text() + "\n💡 переключение: /autotune on|off", None

    if cmd == "info":
        txt = info_full_text() + f"\n\n📥 <a href='{public_url}/whitepaper'><b>Скачать CaptainRost_Whitepaper.txt</b></a>"
        return txt, None

    if cmd == "log":
        return (
            f"📄 <b>Лог работы бота</b>\n\n"
            f"📥 <a href='{public_url}/download-log'><b>Скачать свежий файл лога (bot.log)</b></a>\n"
            f"📊 <a href='{public_url}/audit?hours=48'>Открыть веб-дашборд аудита (48ч)</a>"
        ), None

    return None, None


async def webhook_handler(request):
    """
    Обрабатывает входящие команды и кнопки от Telegram и возвращает ответ
    ПРЯМО В ТЕЛЕ HTTP-ОТВЕТА ВЕБХУКА (мгновенно и без исходящих запросов к api.telegram.org!).
    """
    try:
        data = await request.json()
    except Exception as e:
        logger.error(f"webhook json error: {e}")
        return web.Response(text="OK")

    try:
        # 1. Обработка нажатий инлайн-кнопок (Подтвердить / Отмена)
        if "callback_query" in data:
            cb = data["callback_query"]
            msg = cb.get("message") or {}
            chat_id = msg.get("chat", {}).get("id")
            message_id = msg.get("message_id")
            cb_data = cb.get("data", "")

            if not _is_authorized(chat_id):
                return web.Response(text="OK")

            if cb_data == "cancel":
                return web.json_response({
                    "method": "editMessageText",
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": "❌ Отменено.",
                    "reply_markup": {"inline_keyboard": []},
                })

            if cb_data.startswith("confirm:"):
                key = cb_data.split(":", 1)[1]
                entry = ACTIONS.get(key)
                if entry:
                    fn, _ = entry
                    result = await fn(None)
                    return web.json_response({
                        "method": "editMessageText",
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "text": f"✅ <b>Подтверждено</b>\n\n{result}",
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "reply_markup": {"inline_keyboard": []},
                    })

            return web.Response(text="OK")

        # 2. Обработка текстовых команд (/status, /resetstats, /resetlearn, /start и т.д.)
        msg = data.get("message") or data.get("edited_message")
        if msg:
            chat_id = msg.get("chat", {}).get("id")
            text = (msg.get("text") or "").strip()

            if not _is_authorized(chat_id):
                return web.Response(text="OK")

            if text.startswith("/"):
                parts = text.split()
                cmd = parts[0][1:].split("@")[0].lower()
                args = parts[1:]

                reply_text, markup_dict = await _dispatch_command(cmd, args)
                if reply_text:
                    payload = {
                        "method": "sendMessage",
                        "chat_id": chat_id,
                        "text": reply_text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    }
                    if markup_dict:
                        payload["reply_markup"] = markup_dict
                    return web.json_response(payload)

    except Exception as e:
        logger.exception(f"webhook_handler dispatch error: {e}")

    return web.Response(text="OK")


async def _init_telegram_forever(application):
    """Фоновая синхронизация меню команд и вебхука (НЕ удаляет вебхук и не блокирует бота)."""
    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")
    webhook_url = f"{public_url}{WEBHOOK_PATH}"
    attempt = 0

    while True:
        attempt += 1
        try:
            if not getattr(application, "_initialized", False):
                await application.initialize()
            if not getattr(application, "running", False):
                await application.start()

            await application.bot.set_webhook(
                url=webhook_url,
                drop_pending_updates=False,
                allowed_updates=["message", "edited_message", "callback_query"],
            )

            await application.bot.set_my_commands([
                BotCommand("start", "🚀 Запустить торговлю"),
                BotCommand("pause", "⏸ Пауза"),
                BotCommand("resume", "▶️ Возобновить"),
                BotCommand("status", "📊 Статус"),
                BotCommand("funding", "🏦 Накопительный фонд"),
                BotCommand("chart", "📈 Графики и Аудит 48ч"),
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
            logger.info("✅ Telegram Webhook и меню команд синхронизированы")
            return
        except (TimedOut, NetworkError, Exception) as e:
            wait_sec = min(120.0, 15.0 * attempt)
            logger.debug(f"Фоновая синхронизация Telegram API отложена на {wait_sec:.0f}с ({type(e).__name__})")
            await asyncio.sleep(wait_sec)


async def run_all(application):
    global _app
    _app = application

    web_app = web.Application()
    web_app.router.add_get("/", health_handler)
    web_app.router.add_get("/chart", chart_handler)
    web_app.router.add_get("/audit", audit_handler)
    web_app.router.add_get("/download-log", log_download_handler)
    web_app.router.add_get("/whitepaper", whitepaper_download_handler)
    web_app.router.add_post(WEBHOOK_PATH, webhook_handler)

    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 HTTP-сервер и прямой Webhook-диспетчер запущены на порту {port}")

    asyncio.create_task(_init_telegram_forever(application))
    asyncio.create_task(asyncio.to_thread(ensure_branch))

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

    logger.info("⚡ EDA Core успешно запущено. (DIRECT WEBHOOK REPLY MODE)")

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        # Никогда не вызываем delete_webhook при остановке контейнера, чтобы вебхук оставался активным!
        try:
            if getattr(application, "_initialized", False):
                await application.stop()
                await application.shutdown()
        except Exception:
            pass
        await runner.cleanup()


def main():
    os.makedirs("logs", exist_ok=True)
    logger.add("logs/bot.log", rotation="5 MB", retention="7 days", enqueue=True, level="INFO")

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(15.0)
        .read_timeout(15.0)
        .write_timeout(15.0)
        .pool_timeout(15.0)
        .build()
    )

    asyncio.run(run_all(app))


if __name__ == "__main__":
    main()
