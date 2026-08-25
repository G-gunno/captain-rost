from datetime import datetime, timedelta

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper


def _period_start(kind, tz):
    now = datetime.now(tz)
    if kind == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "weekly":
        return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def build_report(kind, tz):
    prices = await market_data.get_tickers()
    start_ts = int(_period_start(kind, tz).timestamp())
    trades = [r for r in paper.realized if r["time"] >= start_ts]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]

    names = {"daily": "ДНЕВНОЙ ОТЧЁТ", "weekly": "НЕДЕЛЬНЫЙ ОТЧЁТ", "monthly": "МЕСЯЧНЫЙ ОТЧЁТ"}
    msg = [f"📆 {names[kind]} 🕘", ""]
    msg.append(f"💰 Unified (вирт.): {paper.usdt:.2f} USDT")
    msg.append(f"🏦 Funding: {paper.funding:.2f} USDT")
    msg.append(f"📈 Total Equity: {paper.equity(prices):.2f} $")
    msg.append("")
    msg.append(f"🧾 Сделок за период: {len(trades)} (✅ {len(wins)} / ❌ {len(losses)})")
    if trades:
        total = sum(t["pnl"] for t in trades)
        best = max(trades, key=lambda t: t["pnl_pct"])
        worst = min(trades, key=lambda t: t["pnl_pct"])
        msg.append(f"💵 Суммарный PnL: {total:+.2f} USDT")
        msg.append(f"🏆 Лучшая: {best['symbol']} {best['pnl_pct']:+.1f}%")
        msg.append(f"📉 Худшая: {worst['symbol']} {worst['pnl_pct']:+.1f}%")
        msg.append(f"📊 Макс. прибыль: {max(t['pnl_pct'] for t in trades):+.1f}%")
        msg.append(f"📊 Макс. убыток: {min(t['pnl_pct'] for t in trades):+.1f}%")
    else:
        msg.append("Сделок за период не было.")
    if paper.positions:
        msg.append("")
        msg.append("Открытые позиции:")
        for sym, pos in paper.positions.items():
            last = prices.get(sym, {}).get("last", 0)
            pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
            msg.append(f"   {sym}: {pnl_pct:+.1f}%")
    return "\n".join(msg)
