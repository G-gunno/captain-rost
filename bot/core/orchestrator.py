from loguru import logger

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.scanner import get_regime, scan
from bot.strategy.sizing import buy_size
from bot.strategy.indicators import atr

CYCLE_SECONDS = 300
_notify_cb = None


def set_notifier(cb):
    global _notify_cb
    _notify_cb = cb


async def notify(text):
    if _notify_cb:
        try:
            await _notify_cb(text)
        except Exception as e:
            logger.error(f"notify error: {e}")


async def run_cycle():
    logger.info("=== CYCLE START ===")
    tickers = await market_data.get_tickers()
    if not tickers:
        logger.error("Нет тикеров — цикл пропущен")
        return

    # 1. Исполнение ордеров и выходы по TP/SL
    fills = paper.check_fills(tickers)
    for f in fills:
        await notify(f"📥 ИСПОЛНЕНА ПОКУПКА {f['symbol']}: {f['qty']:.6f} @ {f['price']:.6f}\nTP {f['tp']:.6f} | SL {f['sl']:.6f}")
    exits = paper.check_exits(tickers)
    for ex in exits:
        await notify(f"📤 ПРОДАЖА {ex['symbol']} @ {ex['price']:.6f} | {ex['reason']}\nPnL: {ex['pnl']:+.2f} USDT ({ex['pnl_pct']:+.1f}%)\n🏦 В Funding: {ex['transferred']:.2f} USDT")

    # 2. Оценка рынка
    regime, info = await get_regime()
    logger.info(f"Regime: {regime} | {info}")

    # 3. Трейлинг SL — только вверх
    for sym, pos in list(paper.positions.items()):
        t = tickers.get(sym)
        if not t:
            continue
        candles = await market_data.get_kline(sym, "15", 60)
        if len(candles) < 30:
            continue
        a = atr(candles)
        if a <= 0:
            continue
        new_sl = None
        if t["last"] >= pos["avg"] + 1.0 * a:
            new_sl = max(pos["sl"], pos["avg"])
        if t["last"] >= pos["avg"] + 2.0 * a:
            new_sl = max(new_sl or pos["sl"], t["last"] - 0.8 * a)
        if new_sl and new_sl > pos["sl"]:
            pos["sl"] = round(new_sl, 10)
            pos["max_sl"] = max(pos.get("max_sl", 0), pos["sl"])
            paper.save()
            logger.info(f"SL поднят {sym} -> {pos['sl']}")

    # 4. Поиск и покупки
    if regime == "bear":
        logger.info("Медвежий рынок — новые покупки отключены (умный риск)")
    else:
        equity = paper.equity(tickers)
        if paper.usdt >= 10:
            candidates = await scan(regime, tickers)
            for cand in candidates:
                sym = cand["symbol"]
                if sym in paper.positions:
                    continue  # не покупаем повторно, пока не продано
                if any(o["symbol"] == sym for o in paper.orders):
                    continue
                size = buy_size(equity, cand["score"], cand["liquidity"], paper.usdt)
                if size < 5:
                    continue
                entry = cand["last"] * 0.998
                a = cand["atr"]
                if a <= 0:
                    continue
                sl, tp = entry - 1.2 * a, entry + 2.0 * a
                if tp <= entry or sl >= entry:
                    continue
                qty = size / entry
                paper.place_limit_buy(sym, qty, entry, tp=tp, sl=sl)
                await notify(
                    f"🎯 ВЫСТАВЛЕНА ПОКУПКА {sym}\n"
                    f"Сумма: {size:.2f} USDT @ {entry:.8f}\n"
                    f"Score: {cand['score']} | TP {tp:.8f} | SL {sl:.8f}\n"
                    f"Причина: {'; '.join(cand['reasons'][:4])}"
                )
                if paper.usdt < 10:
                    break

    logger.info("=== CYCLE END ===")
