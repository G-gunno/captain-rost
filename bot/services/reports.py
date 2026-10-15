from datetime import datetime, timedelta

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.learner import learner, is_neutral_be
from bot.utils.format import (
    format_coin, sl_emoji, grade_badge,
    usd, fmt_price, fmt_pct, pnl_emoji
)

PERIODS = {
    "daily": ("#Дневной_отчёт", 1),
    "weekly": ("#Недельный_отчёт", 7),
    "monthly": ("#Месячный_отчёт", 30),
}


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

    # Блок 6.1: Выделяем сделки по БУ в нейтральную категорию 🛡🔒
    be_trades = [r for r in trades if r.get("is_be") or is_neutral_be(r.get("pnl_pct", 0.0))]
    decisive = [r for r in trades if r not in be_trades]
    wins = [r for r in decisive if r["pnl"] > 0]
    losses = [r for r in decisive if r["pnl"] <= 0]
    total_pnl = sum(r["pnl"] for r in trades)
    clean_wr = (len(wins) / len(decisive) * 100) if decisive else 0.0

    lines = [f"📅 <b>{title}</b> 🕘", ""]
    free_pct = paper.usdt / eq * 100 if eq else 0
    lines.append(f"💰 Свободно: <b>{usd(paper.usdt)}</b> ({free_pct:.0f}%)")
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

    # Разбивка по типам выходов (Emoji Lexicon)
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

    # Блок 6.2: Изолированная статистика по сетапам [💎] Grade A+
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

    # Сектора (без нейтрального шума БУ)
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
            tp_tag = " · 🔥TP1 · TP🏃🏻‍➡️" if pos.get("tp1_done") else ""
            sl_tag = sl_emoji(pos.get("sl_state", "SHIELD"))
            lines.append(
                f"   {format_coin(sym, pos)}{grade_badge(pos)} · "
                f"{pnl_emoji(pnl_pct)} {fmt_pct(pnl_pct)} · {sl_tag} {fmt_price(pos['sl'])}{tp_tag}"
            )
    else:
        lines.append("📦 <b>Позиции</b>: нет")

    return "\n".join(lines)
