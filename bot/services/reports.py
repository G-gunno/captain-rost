from datetime import datetime, timedelta

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.learner import learner

PERIODS = {
    "daily": ("Дневной отчёт", 1),
    "weekly": ("Недельный отчёт", 7),
    "monthly": ("Месячный отчёт", 30),
}


def usd(x):
    return f"${x:,.2f}"


def pnl_emoji(x):
    return "🟢" if x > 0.05 else ("🔴" if x < -0.05 else "🟡")


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

    trades = [r for r in paper.realized
              if not r.get("partial") and r.get("time", 0) >= start_ts]
    wins = [r for r in trades if r["pnl"] > 0]
    losses = [r for r in trades if r["pnl"] <= 0]
    total_pnl = sum(r["pnl"] for r in trades)

    lines = [f"📅 <b>{title}</b> · 🕐", ""]
    free_pct = paper.usdt / eq * 100 if eq else 0
    lines.append(f"💰 Свободно: <b>{usd(paper.usdt)}</b> ({free_pct:.0f}%)")
    lines.append(f"🏦 Накопления: <b>{usd(paper.funding)}</b>")
    lines.append(f"📈 Капитал: <b>{usd(eq)}</b>")
    lines.append("")

    lines.append(f"📊 <b>Сделки за период</b>: {len(trades)} (✅ {len(wins)} / ❌ {len(losses)})")
    lines.append(f"💵 PnL: {pnl_emoji(total_pnl)} <b>{usd(total_pnl)}</b>")
    if trades:
        best = max(trades, key=lambda r: r["pnl_pct"])
        worst = min(trades, key=lambda r: r["pnl_pct"])
        lines.append(
            f"🏆 Лучшая: <b>{best['symbol'][:-4]}</b> · <i>{best.get('sector', 'Other')}</i> · "
            f"{pnl_emoji(best['pnl_pct'])} {best['pnl_pct']:+.1f}%"
        )
        lines.append(
            f"📉 Худшая: <b>{worst['symbol'][:-4]}</b> · <i>{worst.get('sector', 'Other')}</i> · "
            f"{pnl_emoji(worst['pnl_pct'])} {worst['pnl_pct']:+.1f}%"
        )
    lines.append("")

    # Сектора за период
    sector_agg = {}
    for r in trades:
        sector_agg.setdefault(r.get("sector", "Other"), []).append(r["pnl_pct"])
    lines.append("🧭 <b>Сектора за период</b>")
    if sector_agg:
        rows = [(s, len(v), sum(v) / len(v)) for s, v in sector_agg.items()]
        rows.sort(key=lambda x: x[2], reverse=True)
        for s, cnt, avg in rows[:6]:
            lines.append(f"   {pnl_emoji(avg)} <i>{s}</i> · {cnt} · ср. {avg:+.2f}%")
    else:
        lines.append("   (нет закрытых сделок)")
    lines.append("")

    # Стиль: core vs сателлиты
    kind_agg = {}
    for r in trades:
        kind_agg.setdefault(r.get("kind", "core"), []).append(r["pnl_pct"])
    lines.append("🏛/🛰 <b>Стиль за период</b>")
    if kind_agg:
        for k, label in (("core", "🏛 Core"), ("satellite", "🛰 Сателлиты")):
            v = kind_agg.get(k)
            if v:
                avg = sum(v) / len(v)
                lines.append(f"   {pnl_emoji(avg)} {label} · {len(v)} · ср. {avg:+.2f}%")
    else:
        lines.append("   (нет закрытых сделок)")
    lines.append(f"   🛰 Лимит сателлитов сейчас: <b>{learner.satellite_limit():.0f}%</b>")
    lines.append("")

    # Открытые позиции
    if paper.positions:
        lines.append(f"📦 <b>Открытые позиции ({len(paper.positions)})</b>")
        for sym, pos in paper.positions.items():
            last = prices.get(sym, {}).get("last", 0)
            pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] and last else 0
            kind = "🛰" if pos.get("kind") == "satellite" else "🏛"
            sector = pos.get("sector") or "Other"
            tp1 = " · 🔥TP1" if pos.get("tp1_done") else ""
            lines.append(
                f"   {kind} <b>{sym[:-4]}</b> · <i>{sector}</i> · "
                f"{pnl_emoji(pnl_pct)} {pnl_pct:+.1f}%{tp1}"
            )
    else:
        lines.append("📦 <b>Открытые позиции</b>: нет")

    return "\n".join(lines)
