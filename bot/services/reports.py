import html as _html
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.learner import learner, is_neutral_be
from bot.news.cmc import TIER_EMOJI
from bot.core.state import bot_state
from bot.utils.format import (
    format_coin, sl_emoji, grade_badge, mode_emoji,
    usd, fmt_price, fmt_pct, pnl_emoji
)

PERIODS = {
    "daily": ("#Дневной_отчёт", 1),
    "weekly": ("#Недельный_отчёт", 7),
    "monthly": ("#Месячный_отчёт", 30),
}


def _fmt_ts(ts: int, tz) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz).strftime("%H:%M")


async def build_audit_html(tz, public_url: str = "") -> str:
    """
    Генерирует интерактивный веб-дашборд инфографики за 24 часа (/audit):
- KPI-карточки в шапке
- 3 интерактивных графика Plotly (Кривая капитала 24ч, PnL по секторам, Донат типов выходов)
- 3 интерактивные таблицы (Открытые позиции и ордера, Рейтинг монет, Хронология сделок)
      с прямыми кликабельными ссылками на свечной график каждой монеты (/chart?symbol=...).
    """
    prices = await market_data.get_tickers()
    eq = paper.equity(prices)
    now = datetime.now(tz)
    cutoff = int(time.time()) - 86400

    locked_usdt = sum(o["qty"] * o["price"] for o in paper.orders)
    avail_usdt = max(0.0, paper.usdt - locked_usdt)
    metrics_24h = paper.get_metrics(prices, hours=24)
    a_stats = learner.get_grade_a_stats()

    closed_24h = [
        r for r in paper.realized
        if not r.get("partial") and r.get("time", 0) >= cutoff
    ]
    closed_24h.sort(key=lambda x: x.get("time", 0))

    be_trades = [r for r in closed_24h if r.get("is_be") or is_neutral_be(r.get("pnl_pct", 0.0))]
    decisive = [r for r in closed_24h if r not in be_trades]
    wins = [r for r in decisive if r["pnl"] > 0]
    losses = [r for r in decisive if r["pnl"] <= 0]
    clean_wr = (len(wins) / len(decisive) * 100) if decisive else 0.0

    # --- ДАННЫЕ ДЛЯ ГРАФИКОВ PLOTLY ---
    # 1. Кривая накопленного PnL ($) за 24ч
    eq_times = ["Старт 24ч"]
    eq_vals = [0.0]
    eq_texts = ["Начало отсчёта"]
    cum_pnl = 0.0
    for t in closed_24h:
        cum_pnl = round(cum_pnl + float(t.get("pnl", 0.0)), 2)
        sym_b = t["symbol"][:-4] if t["symbol"].endswith("USDT") else t["symbol"]
        eq_times.append(_fmt_ts(t.get("time"), tz) + f" ({sym_b})")
        eq_vals.append(cum_pnl)
        eq_texts.append(f"{sym_b}: {t['pnl_pct']:+.2f}% ({usd(t['pnl'])}) · {t.get('exit_type', '')}")

    # 2. PnL по секторам ($)
    sec_pnl = defaultdict(float)
    sec_cnt = defaultdict(int)
    for t in closed_24h:
        s = t.get("sector", "Other")
        sec_pnl[s] += float(t.get("pnl", 0.0))
        sec_cnt[s] += 1
    sec_sorted = sorted(sec_pnl.items(), key=lambda kv: kv[1])
    sec_labels = [f"{k} ({sec_cnt[k]})" for k, _ in sec_sorted] or ["Нет данных"]
    sec_values = [round(v, 2) for _, v in sec_sorted] or [0.0]
    sec_colors = ["#00e676" if v >= 0 else "#ff1744" for v in sec_values]

    # 3. Распределение выходов
    exit_cnt = defaultdict(int)
    for t in closed_24h:
        exit_cnt[t.get("exit_type", "🛡📉")] += 1
    exit_labels = list(exit_cnt.keys()) or ["Нет сделок"]
    exit_values = list(exit_cnt.values()) or [1]

    # --- ТАБЛИЦА 1: ОТКРЫТЫЕ ПОЗИЦИИ И ЛИМИТНЫЕ ОРДЕРА ---
    active_rows_html = []
    for sym, pos in sorted(paper.positions.items()):
        sym_base = sym[:-4] if sym.endswith("USDT") else sym
        last_p = prices.get(sym, {}).get("last", 0)
        val = pos["qty"] * last_p
        p_pct = (last_p - pos["avg"]) / pos["avg"] * 100 if pos.get("avg") and last_p else 0.0
        pnl_cls = "pos-val" if p_pct >= 0 else "neg-val"
        g_badge = "<span class='badge badge-a'>💎 A+</span>" if pos.get("grade") == "A+" else "<span class='badge badge-b'>B</span>"
        tp1_badge = "<span class='badge badge-tp1'>🔥 TP1</span>" if pos.get("tp1_done") else ""
        sl_tag = sl_emoji(pos.get("sl_state", "SHIELD"))
        sc_s = f"{pos.get('score', 0):.1f} / {pos.get('thr', 0):.2f}" if pos.get("score") else "—"
        r1h = _html.escape(f"{pos.get('gate_1h', '')} ({pos.get('reason_1h', '')})")
        corr_s = f"₿ {pos.get('corr', 0) or 0:.2f}"
        why_p = _html.escape("; ".join(pos.get("reasons", [])[:5]) or ", ".join(pos.get("reason_keys", [])))

        active_rows_html.append(
            f"<tr>"
            f"<td><span class='badge badge-open'>📦 ПОЗИЦИЯ</span></td>"
            f"<td><a class='coin-link' href='/chart?symbol={sym}&interval=60' target='_blank'>{mode_emoji(pos)} <b>{sym_base}</b></a> {TIER_EMOJI.get(pos.get('tier'), '🐭')} <span class='muted'>({pos.get('sector', 'Other')})</span></td>"
            f"<td>{pos.get('entry_mode', 'sniper')} {g_badge} {tp1_badge}</td>"
            f"<td><b>{usd(val)}</b></td>"
            f"<td>${fmt_price(pos['avg'])} &rarr; <b>${fmt_price(last_p)}</b> (<span class='{pnl_cls}'>{p_pct:+.2f}%</span>)</td>"
            f"<td>🎯 ${fmt_price(pos['tp'])}<br>{sl_tag} <b>${fmt_price(pos['sl'])}</b></td>"
            f"<td>{sc_s}</td>"
            f"<td>{r1h}<br><span class='muted'>{corr_s}</span></td>"
            f"<td class='reasons-cell'>{why_p}</td>"
            f"</tr>"
        )

    for o in paper.orders:
        sym = o["symbol"]
        sym_base = sym[:-4] if sym.endswith("USDT") else sym
        last_p = prices.get(sym, {}).get("last", 0)
        val = o["qty"] * o["price"]
        dist = (o["price"] - last_p) / last_p * 100 if last_p > 0 else 0.0
        g_badge = "<span class='badge badge-a'>💎 A+</span>" if o.get("grade") == "A+" else "<span class='badge badge-b'>B</span>"
        sc_s = f"{o.get('score', 0):.1f} / {o.get('thr', 0):.2f}" if o.get("score") else "—"
        r1h = _html.escape(f"{o.get('gate_1h', '')} ({o.get('reason_1h', '')})")
        corr_s = f"₿ {o.get('corr', 0) or 0:.2f}"
        why_o = _html.escape("; ".join(o.get("reasons", [])[:5]) or ", ".join(o.get("reason_keys", [])))

        active_rows_html.append(
            f"<tr>"
            f"<td><span class='badge badge-order'>📋 ОРДЕР</span></td>"
            f"<td><a class='coin-link' href='/chart?symbol={sym}&interval=60' target='_blank'>{mode_emoji(o)} <b>{sym_base}</b></a> {TIER_EMOJI.get(o.get('tier'), '🐭')} <span class='muted'>({o.get('sector', 'Other')})</span></td>"
            f"<td>{o.get('entry_mode', 'sniper')} {g_badge}</td>"
            f"<td><b>{usd(val)}</b></td>"
            f"<td>Лимитка <b>${fmt_price(o['price'])}</b> <span class='muted'>({dist:+.2f}% от ${fmt_price(last_p)})</span></td>"
            f"<td>🎯 ${fmt_price(o['tp'])}<br>🛡📉 ${fmt_price(o['sl'])}</td>"
            f"<td>{sc_s}</td>"
            f"<td>{r1h}<br><span class='muted'>{corr_s}</span></td>"
            f"<td class='reasons-cell'>{why_o}</td>"
            f"</tr>"
        )

    if not active_rows_html:
        active_rows_html.append("<tr><td colspan='9' class='empty-row'>Открытых позиций и активных ордеров сейчас нет</td></tr>")

    # --- ТАБЛИЦА 2: СВОДНЫЙ РЕЕСТР ПО МОНЕТАМ ЗА 24Ч ---
    by_coin = defaultdict(list)
    for t in closed_24h:
        by_coin[t["symbol"]].append(t)
    for sym in paper.positions:
        by_coin.setdefault(sym, [])

    coin_rows = []
    for sym, t_list in by_coin.items():
        sum_pct = sum(x["pnl_pct"] for x in t_list)
        sum_usd = sum(x["pnl"] for x in t_list)
        sample = t_list[-1] if t_list else paper.positions.get(sym, {})
        sec = sample.get("sector", "Other")
        t_em = TIER_EMOJI.get(sample.get("tier"), "🐭")

        trades_desc = []
        for x in t_list:
            ic = "🛡🔒" if (x.get("is_be") or is_neutral_be(x["pnl_pct"])) else ("✅" if x["pnl"] > 0 else "❌")
            gb = "💎" if x.get("grade") == "A+" else ""
            cls_c = "pos-val" if x["pnl"] > 0 else ("neg-val" if x["pnl"] < 0 else "muted")
            trades_desc.append(f"{ic} {mode_emoji(x)}{gb} <span class='{cls_c}'>{x['pnl_pct']:+.2f}%</span> ({x.get('exit_type', '')})")
        trades_str = " &middot; ".join(trades_desc) if trades_desc else "<span class='muted'>—</span>"

        pos = paper.positions.get(sym)
        if pos:
            last_p = prices.get(sym, {}).get("last", 0)
            p_pct = (last_p - pos["avg"]) / pos["avg"] * 100 if pos.get("avg") and last_p else 0.0
            p_cls = "pos-val" if p_pct >= 0 else "neg-val"
            pos_str = f"{mode_emoji(pos)}{grade_badge(pos)} <span class='{p_cls}'>{p_pct:+.2f}%</span> ({sl_emoji(pos.get('sl_state'))})"
        else:
            pos_str = "<span class='muted'>—</span>"

        coin_rows.append((sym, sec, t_em, len(t_list), trades_str, pos_str, sum_pct, sum_usd))

    coin_rows.sort(key=lambda r: r[6], reverse=True)
    coin_rows_html = []
    for sym, sec, t_em, cnt, trades_str, pos_str, sum_pct, sum_usd in coin_rows:
        sym_base = sym[:-4] if sym.endswith("USDT") else sym
        tot_cls = "pos-val" if sum_pct > 0.15 else ("neg-val" if sum_pct < -0.15 else "muted")
        coin_rows_html.append(
            f"<tr>"
            f"<td><a class='coin-link' href='/chart?symbol={sym}&interval=60' target='_blank'>📈 <b>{sym_base}</b></a></td>"
            f"<td>{sec} {t_em}</td>"
            f"<td style='text-align:center;'><b>{cnt}</b></td>"
            f"<td>{trades_str}</td>"
            f"<td>{pos_str}</td>"
            f"<td><span class='{tot_cls}'><b>{sum_pct:+.2f}%</b> ({usd(sum_usd)})</span></td>"
            f"</tr>"
        )

    if not coin_rows_html:
        coin_rows_html.append("<tr><td colspan='6' class='empty-row'>Нет данных по монетам за 24 часа</td></tr>")

    # --- ТАБЛИЦА 3: ХРОНОЛОГИЧЕСКИЙ ЖУРНАЛ ВСЕХ СДЕЛОК (24Ч) ---
    trades_rows_html = []
    for idx, t in enumerate(closed_24h, 1):
        sym = t["symbol"]
        sym_base = sym[:-4] if sym.endswith("USDT") else sym
        t_em = TIER_EMOJI.get(t.get("tier"), "🐭")
        sec = t.get("sector", "Other")
        m_em = mode_emoji(t)
        emode = t.get("entry_mode", "sniper")
        g_badge = "<span class='badge badge-a'>💎 A+</span>" if t.get("grade") == "A+" else "<span class='badge badge-b'>B</span>"
        reg_str = (t.get("regime") or "neutral").upper()
        rmode = t.get("risk_mode", "NORMAL")

        t_ord = _fmt_ts(t.get("order_created"), tz)
        t_in = _fmt_ts(t.get("entry_time"), tz)
        t_out = _fmt_ts(t.get("time"), tz)

        sc = t.get("score", 0.0)
        thr = t.get("thr", 0.0)
        sc_str = f"<b>{sc:.1f}</b> / {thr:.2f}" if sc > 0 else "—"

        g1h = _html.escape(t.get("gate_1h") or "1H_OK")
        r1h = _html.escape(t.get("reason_1h") or "—")
        corr_v = t.get("corr")
        corr_s = f"₿ {corr_v:.2f}" if corr_v is not None else ""

        reasons_list = t.get("reasons") or t.get("reason_keys") or []
        why_str = _html.escape("; ".join(str(x) for x in reasons_list[:6])) if reasons_list else "—"

        ep = t.get("entry_price")
        xp = t.get("exit_price")
        itp = t.get("initial_tp")
        isl = t.get("initial_sl")
        if ep and xp:
            prices_str = f"${fmt_price(ep)} &rarr; <b>${fmt_price(xp)}</b>"
            if itp and isl:
                prices_str += f"<br><span class='muted'>TP ${fmt_price(itp)} / SL ${fmt_price(isl)}</span>"
        else:
            prices_str = "—"

        is_be_flag = t.get("is_be") or is_neutral_be(t["pnl_pct"])
        res_icon = "🛡🔒" if is_be_flag else ("✅" if t["pnl"] > 0 else "❌")
        pnl_cls = "pos-val" if t["pnl"] > 0 else ("neg-val" if t["pnl"] < 0 else "muted")
        tp1_mark = " + <span class='badge badge-tp1'>🔥TP1</span>" if t.get("tp1_done") else ""

        trades_rows_html.append(
            f"<tr>"
            f"<td style='text-align:center;'>{idx}</td>"
            f"<td><a class='coin-link' href='/chart?symbol={sym}&interval=60' target='_blank'>📈 <b>{sym_base}</b></a> {t_em}<br><span class='muted'>{sec}</span></td>"
            f"<td>{t_ord} / {t_in} / <b>{t_out}</b></td>"
            f"<td>{m_em} {emode} {g_badge}</td>"
            f"<td>{reg_str}<br><span class='muted'>{rmode}</span></td>"
            f"<td>{sc_str}</td>"
            f"<td><b>{g1h}</b><br><span class='muted'>{r1h} {corr_s}</span></td>"
            f"<td class='reasons-cell'>{why_str}</td>"
            f"<td>{prices_str}</td>"
            f"<td>{res_icon} <span class='{pnl_cls}'><b>{t['pnl_pct']:+.2f}%</b> ({usd(t['pnl'])})</span><br>{t.get('exit_type', '')}{tp1_mark}</td>"
            f"</tr>"
        )

    if not trades_rows_html:
        trades_rows_html.append("<tr><td colspan='10' class='empty-row'>За последние 24 часа закрытых сделок нет</td></tr>")

    pf_val = metrics_24h["profit_factor"]
    pf_str = "—" if pf_val is None else ("∞" if pf_val == float("inf") else f"{pf_val:.2f}")
    tot_pnl_24 = metrics_24h["total_pnl"]
    tot_pnl_cls = "pos-val" if tot_pnl_24 >= 0 else "neg-val"
    curr_reg = bot_state.current_regime.upper()

    html_page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Капитан Рост — Интерактивный Аудит 24ч</title>
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
  :root {{
    --bg: #0f1218;
    --card: #171c26;
    --border: #262f40;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #2962ff;
    --green: #00e676;
    --red: #ff5252;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 20px;
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    font-size: 13.5px;
  }}
  .header-bar {{
    display: flex; justify-content: space-between; align-items: center;
    background: var(--card); border: 1px solid var(--border);
    padding: 14px 20px; border-radius: 10px; margin-bottom: 18px;
    flex-wrap: wrap; gap: 10px;
  }}
  .header-title {{ font-size: 18px; font-weight: 700; }}
  .btn {{
    text-decoration: none; color: #fff; background: var(--accent);
    padding: 8px 14px; border-radius: 6px; font-weight: 600; font-size: 13px;
  }}
  .kpi-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(185px, 1fr));
    gap: 12px; margin-bottom: 18px;
  }}
  .kpi-card {{
    background: var(--card); border: 1px solid var(--border);
    padding: 12px 15px; border-radius: 8px;
  }}
  .kpi-label {{ color: var(--muted); font-size: 12px; margin-bottom: 5px; }}
  .kpi-val {{ font-size: 18px; font-weight: 700; }}
  .charts-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
    gap: 14px; margin-bottom: 22px;
  }}
  .chart-box {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 8px; min-height: 300px;
  }}
  .section-title {{
    font-size: 15px; font-weight: 700; margin: 22px 0 10px;
    padding-left: 10px; border-left: 3px solid var(--accent);
  }}
  .table-wrap {{
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; overflow-x: auto; margin-bottom: 20px;
  }}
  table {{ width: 100%; border-collapse: collapse; min-width: 850px; }}
  th, td {{
    padding: 9px 11px; text-align: left;
    border-bottom: 1px solid var(--border); vertical-align: top;
  }}
  th {{ background: #1d2432; color: #b8c4d4; font-weight: 600; font-size: 12px; text-transform: uppercase; }}
  tr:hover td {{ background: rgba(41, 98, 255, 0.06); }}
  .coin-link {{
    color: #58a6ff; text-decoration: none;
    padding: 2px 6px; border-radius: 4px; background: rgba(88, 166, 255, 0.12);
  }}
  .coin-link:hover {{ background: rgba(88, 166, 255, 0.28); }}
  .pos-val {{ color: var(--green); }}
  .neg-val {{ color: var(--red); }}
  .muted {{ color: var(--muted); font-size: 12px; }}
  .badge {{
    display: inline-block; padding: 2px 6px; border-radius: 4px;
    font-size: 11px; font-weight: 700;
  }}
  .badge-a {{ background: rgba(0, 229, 255, 0.16); color: #00e5ff; border: 1px solid rgba(0, 229, 255, 0.4); }}
  .badge-b {{ background: rgba(139, 148, 158, 0.15); color: #8b949e; }}
  .badge-tp1 {{ background: rgba(0, 230, 118, 0.18); color: #00e676; }}
  .badge-open {{ background: rgba(41, 98, 255, 0.22); color: #82b1ff; }}
  .badge-order {{ background: rgba(255, 171, 0, 0.2); color: #ffd740; }}
  .reasons-cell {{ max-width: 340px; font-size: 12.5px; line-height: 1.4; }}
  .empty-row {{ text-align: center; color: var(--muted); padding: 18px !important; }}
</style>
</head>
<body>

<div class="header-bar">
  <div>
    <div class="header-title">📊 Капитан Рост — Интерактивный Аудит и Инфографика (24ч)</div>
    <div class="muted">Срез: {now.strftime('%Y-%m-%d %H:%M:%S %Z')} · Клик по любой монете открывает её интерактивный свечной график</div>
  </div>
  <div>
    <a class="btn" href="/audit">🔄 Обновить срез</a>
  </div>
</div>

<div class="kpi-grid">
  <div class="kpi-card">
    <div class="kpi-label">📈 Торговый капитал</div>
    <div class="kpi-val">{usd(eq)}</div>
    <div class="muted">Свободно: {usd(avail_usdt)} · Ордера: {usd(locked_usdt)}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">💵 PnL за 24 часа</div>
    <div class="kpi-val {tot_pnl_cls}">{usd(tot_pnl_24)}</div>
    <div class="muted">🏦 Накопления: {usd(paper.funding)}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">🎯 Чистый Winrate (24ч)</div>
    <div class="kpi-val">{clean_wr:.1f}% <span class="muted">({len(closed_24h)} сдел.)</span></div>
    <div class="muted">✅ {len(wins)} · ❌ {len(losses)} · 🛡🔒 {len(be_trades)} · 🔥TP1: {metrics_24h['partial_count']}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">📊 Profit Factor & Max DD</div>
    <div class="kpi-val">PF: {pf_str} · DD: {metrics_24h['max_drawdown_pct']:.1f}%</div>
    <div class="muted">Цель PF ≥ 1.3 · Лимит DD 15%</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">💎 Сетапы [💎] Grade A+</div>
    <div class="kpi-val">WR {a_stats['wr']:.0%} <span class="muted">(×{a_stats['mult']:.2f})</span></div>
    <div class="muted">Реш.: {a_stats['decisive']} · Серия: {a_stats['streak']:+d}</div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">🧭 Режим рынка и ИИ</div>
    <div class="kpi-val">{curr_reg} · {learner.current_risk_mode}</div>
    <div class="muted">Строгость ИИ: {learner.threshold_adj:+.2f}</div>
  </div>
</div>

<div class="charts-grid">
  <div class="chart-box" id="chart-equity"></div>
  <div class="chart-box" id="chart-sectors"></div>
  <div class="chart-box" id="chart-exits"></div>
</div>

<div class="section-title">1. Текущие открытые позиции ({len(paper.positions)}) и активные лимитные ордера ({len(paper.orders)})</div>
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>Статус</th><th>Монета (График)</th><th>Стратегия / Грейд</th><th>Объём $</th>
        <th>Вход &rarr; Текущая (PnL)</th><th>TP / SL (Фаза)</th><th>Скор / Порог</th>
        <th>1H Валидация (48ч)</th><th>Причины и триггеры входа</th>
      </tr>
    </thead>
    <tbody>
      {"".join(active_rows_html)}
    </tbody>
  </table>
</div>

<div class="section-title">2. Сводный реестр по всем монетам за 24 часа (от лучших к худшим)</div>
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>Монета (График)</th><th>Сектор / Тир</th><th style="text-align:center;">Сделок</th>
        <th>Все закрытые сделки за 24ч</th><th>Открытая позиция сейчас</th><th>Суммарный PnL (% / $)</th>
      </tr>
    </thead>
    <tbody>
      {"".join(coin_rows_html)}
    </tbody>
  </table>
</div>

<div class="section-title">3. Детальный хронологический журнал всех закрытых сделок за 24 часа ({len(closed_24h)})</div>
<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>№</th><th>Монета (График)</th><th>Ордер / Вход / Выход</th><th>Стратегия / Грейд</th>
        <th>Рынок / Режим</th><th>Скор / Порог</th><th>Валидация 1H (48ч) и ₿</th>
        <th>Почему решили купить (Триггеры)</th><th>Вход &rarr; Выход (TP / SL)</th><th>Итог PnL и Выход</th>
      </tr>
    </thead>
    <tbody>
      {"".join(trades_rows_html)}
    </tbody>
  </table>
</div>

<script>
  const darkLayout = {{
    paper_bgcolor: '#171c26', plot_bgcolor: '#171c26',
    font: {{ color: '#e6edf3', size: 11 }},
    margin: {{ t: 38, b: 38, l: 45, r: 20 }},
    height: 290
  }};

  Plotly.newPlot('chart-equity', [{{
    x: {json.dumps(eq_times, ensure_ascii=False)},
    y: {json.dumps(eq_vals)},
    text: {json.dumps(eq_texts, ensure_ascii=False)},
    mode: 'lines+markers',
    line: {{ color: '#00e5ff', width: 2.5, shape: 'hv' }},
    marker: {{ size: 7, color: '#2962ff' }},
    fill: 'tozeroy',
    fillcolor: 'rgba(0, 229, 255, 0.08)',
    hovertemplate: '%{{text}}<br>Кумулятивный PnL: <b>$%{{y:.2f}}</b><extra></extra>'
  }}], Object.assign({{}}, darkLayout, {{ title: 'Кривая кумулятивного PnL ($) за 24ч' }}), {{responsive: true, displayModeBar: false}});

  Plotly.newPlot('chart-sectors', [{{
    x: {json.dumps(sec_values)},
    y: {json.dumps(sec_labels, ensure_ascii=False)},
    type: 'bar', orientation: 'h',
    marker: {{ color: {json.dumps(sec_colors)} }},
    hovertemplate: '%{{y}}: <b>$%{{x:.2f}}</b><extra></extra>'
  }}], Object.assign({{}}, darkLayout, {{ title: 'PnL по секторам ($) за 24ч', margin: {{ t: 38, b: 35, l: 95, r: 20 }} }}), {{responsive: true, displayModeBar: false}});

  Plotly.newPlot('chart-exits', [{{
    labels: {json.dumps(exit_labels, ensure_ascii=False)},
    values: {json.dumps(exit_values)},
    type: 'pie', hole: 0.45,
    textinfo: 'label+value',
    margin: {{ t: 38, b: 20, l: 20, r: 20 }}
  }}], Object.assign({{}}, darkLayout, {{ title: 'Структура типов выхода (24ч)', showlegend: true }}), {{responsive: true, displayModeBar: false}});
</script>
</body>
</html>"""
    return html_page


async def build_report(period, tz):
    title, days = PERIODS.get(period, PERIODS["daily"])
    prices = await market_data.get_tickers()
    eq = paper.equity(prices)
    now = datetime.now(tz)

    if period == "daily":
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start_dt = now - timedelta(days=days)
    start_ts = int(start_dt.timestamp())

    trades = [
        r for r in paper.realized
        if not r.get("partial") and r.get("time", 0) >= start_ts
    ]

    be_trades = [r for r in trades if r.get("is_be") or is_neutral_be(r.get("pnl_pct", 0.0))]
    decisive = [r for r in trades if r not in be_trades]
    wins = [r for r in decisive if r["pnl"] > 0]
    losses = [r for r in decisive if r["pnl"] <= 0]
    total_pnl = sum(r["pnl"] for r in trades)
    clean_wr = (len(wins) / len(decisive) * 100) if decisive else 0.0

    locked_usdt = sum(o["qty"] * o["price"] for o in paper.orders)
    avail_usdt = max(0.0, paper.usdt - locked_usdt)
    free_pct = avail_usdt / eq * 100 if eq else 0

    lines = [f"📅 <b>{title}</b> 🕘", ""]
    lines.append(f"💰 Свободно: <b>{usd(avail_usdt)}</b> ({free_pct:.0f}%)")
    if locked_usdt > 0:
        lines.append(f"🔒 В ордерах: <b>{usd(locked_usdt)}</b>")
    lines.append(f"🏦 Накопления: <b>{usd(paper.funding)}</b>")
    lines.append(f"📈 Капитал: <b>{usd(eq)}</b>")
    lines.append("")

    history = [h for h in getattr(paper, "market_history", []) if h.get("ts", 0) >= start_ts]
    if history:
        total_h = len(history)
        r_counts, m_counts = {}, {}
        for h in history:
            r, m = h.get("regime", "neutral"), h.get("mode", "NORMAL")
            r_counts[r] = r_counts.get(r, 0) + 1
            m_counts[m] = m_counts.get(m, 0) + 1

        r_map = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}
        m_map = {"NORMAL": "🟢 NORMAL", "CAUTIOUS": "🟡 CAUTIOUS", "STRICT": "🔴 STRICT", "AGGRESSIVE": "🚀 AGGRESSIVE"}

        r_str = " · ".join(f"{r_map.get(k, k)} {v/total_h*100:.0f}%" for k, v in sorted(r_counts.items(), key=lambda x: x[1], reverse=True))
        m_str = " · ".join(f"{m_map.get(k, k)} {v/total_h*100:.0f}%" for k, v in sorted(m_counts.items(), key=lambda x: x[1], reverse=True))

        lines.append(f"🌍 <b>Фон:</b> 🧭 {r_str} | 🎚 {m_str}")
        lines.append("")

    lines.append(
        f"📊 <b>Сделки ({len(trades)}):</b> ✅ {len(wins)} · ❌ {len(losses)} · 🛡🔒 {len(be_trades)} "
        f"<i>(чистый WR {clean_wr:.0f}%)</i>"
    )
    lines.append(f"💵 PnL: {pnl_emoji(total_pnl)} <b>{usd(total_pnl)}</b>")

    if trades:
        exit_counts = {}
        for r in trades:
            et = r.get("exit_type", "🛡📉")
            exit_counts[et] = exit_counts.get(et, 0) + 1
        ex_str = " · ".join(f"{k}: {v}" for k, v in sorted(exit_counts.items(), key=lambda kv: kv[1], reverse=True))
        lines.append(f"🚪 <b>Выходы:</b> {ex_str}")

    if decisive:
        best = max(decisive, key=lambda r: r["pnl_pct"])
        worst = min(decisive, key=lambda r: r["pnl_pct"])
        lines.append(f"🏆 {format_coin(best['symbol'], best)}{grade_badge(best)} · {pnl_emoji(best['pnl_pct'])} {best['pnl_pct']:+.1f}% ({best.get('exit_type', '')})")
        lines.append(f"📉 {format_coin(worst['symbol'], worst)}{grade_badge(worst)} · {pnl_emoji(worst['pnl_pct'])} {worst['pnl_pct']:+.1f}% ({worst.get('exit_type', '')})")
    lines.append("")

    a_trades = [r for r in trades if r.get("grade") == "A+"]
    a_stats_all = learner.get_grade_a_stats()
    lines.append("💎 <b>Элитные сетапы [💎]</b>")
    if a_trades:
        a_be = [r for r in a_trades if r.get("is_be") or is_neutral_be(r.get("pnl_pct", 0.0))]
        a_dec = [r for r in a_trades if r not in a_be]
        a_wins = [r for r in a_dec if r["pnl"] > 0]
        a_wr = (len(a_wins) / len(a_dec) * 100) if a_dec else 0.0
        a_pnl = sum(r["pnl"] for r in a_trades)
        lines.append(
            f"   За период: {len(a_trades)} (✅ {len(a_wins)} / ❌ {len(a_dec)-len(a_wins)} / 🛡🔒 {len(a_be)}) "
            f"· WR {a_wr:.0f}% · {usd(a_pnl)}"
        )
    lines.append(
        f"   ИИ-доверие [💎]: <b>×{a_stats_all['mult']:.2f}</b> "
        f"(серия {a_stats_all['streak']:+d} · общий WR {a_stats_all['wr']:.0%} за {a_stats_all['decisive']} сдел.)"
    )
    lines.append("")

    sector_agg = {}
    for r in decisive:
        sector_agg.setdefault(r.get("sector", "Other"), []).append(r["pnl_pct"])
    lines.append("🧭 <b>Сектора</b>")
    if sector_agg:
        rows = [(s, len(v), sum(v) / len(v)) for s, v in sector_agg.items()]
        rows.sort(key=lambda x: x[2], reverse=True)
        for s, cnt, avg in rows[:6]:
            lines.append(f"   {pnl_emoji(avg)} <i>{s}</i> · {cnt} · {avg:+.2f}%")
    else:
        lines.append("   <i>(нет результативных сделок)</i>")
    lines.append("")

    if paper.positions:
        lines.append(f"📦 <b>Позиции ({len(paper.positions)})</b>")
        for sym, pos in paper.positions.items():
            last = prices.get(sym, {}).get("last", 0)
            pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] and last else 0
            tp_tag = " · 🔥 TP1 · TP🏃🏻‍➡️" if pos.get("tp1_done") else ""
            sl_tag = sl_emoji(pos.get("sl_state", "SHIELD"))
            lines.append(
                f"   {format_coin(sym, pos)}{grade_badge(pos)} · "
                f"{pnl_emoji(pnl_pct)} {fmt_pct(pnl_pct)} · {sl_tag} {fmt_price(pos['sl'])}{tp_tag}"
            )
    else:
        lines.append("📦 <b>Позиции</b>: нет")

    return "\n".join(lines)
