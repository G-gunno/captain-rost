import time

from loguru import logger

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.scanner import get_regime, scan
from bot.strategy.sizing import buy_size
from bot.strategy.indicators import atr
from bot.core.state import bot_state
from bot.utils.format import fmt_price, fmt_usdt, fmt_pct, fmt_sym

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
    if bot_state.paused or not bot_state.trading_enabled:
        logger.info("Цикл пропущен: торговля на паузе или остановлена")
        return

    logger.info("=== CYCLE START ===")
    tickers = await market_data.get_tickers()
    if not tickers:
        logger.error("Нет тикеров — цикл пропущен")
        return

    # 1. Исполнения и выходы по TP/SL
    for f in paper.check_fills(tickers):
        tp_pct = (f["tp"] - f["price"]) / f["price"] * 100
        sl_pct = (f["sl"] - f["price"]) / f["price"] * 100
        await notify(
            f"📥 ИСПОЛНЕНА ПОКУПКА · {fmt_sym(f['symbol'])}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 {fmt_usdt(f['qty'] * f['price'])} USDT @ {fmt_price(f['price'])}\n"
            f"🎯 TP: {fmt_price(f['tp'])} ({fmt_pct(tp_pct)})\n"
            f"🛡 SL: {fmt_price(f['sl'])} ({fmt_pct(sl_pct)})"
        )
    for ex in paper.check_exits(tickers):
        await notify(
            f"📤 ПРОДАЖА · {fmt_sym(ex['symbol'])} · {ex['reason']}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Цена: {fmt_price(ex['price'])} ({fmt_pct(ex['pnl_pct'])})\n"
            f"💵 PnL: {ex['pnl']:+.2f} USDT\n"
            f"🏦 В Funding: {fmt_usdt(ex['transferred'])} USDT"
        )

    # 2. Оценка рынка
    regime, info = await get_regime()
    logger.info(f"Regime: {regime} | {info}")

    # 3. ЭКСТРЕННЫЙ РИСК-МЕНЕДЖМЕНТ
    btc_t = tickers.get("BTCUSDT", {})
    btc_c = await market_data.get_kline("BTCUSDT", "60", 3)
    drop_1h = 0.0
    if len(btc_c) >= 2 and btc_c[-2]["close"] > 0:
        drop_1h = (btc_c[-1]["close"] - btc_c[-2]["close"]) / btc_c[-2]["close"] * 100
    if drop_1h <= -3 or btc_t.get("change_pct", 0) <= -6:
        if paper.positions or paper.orders:
            for ex in paper.sell_all(tickers):
                await notify(
                    f"🚨 ЭКСТРЕННЫЙ ВЫХОД · {fmt_sym(ex['symbol'])}\n"
                    f"💰 {fmt_price(ex['price'])} | PnL: {ex['pnl']:+.2f} USDT"
                )
            await notify("🚨 Риск-менеджмент: резкий дамп рынка. Все позиции закрыты в USDT.")
        return

    # 4. Трейлинг SL — только вверх
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

    # 5. Неисполненные ордера: перевыставление
    now = int(time.time())
    for order in list(paper.orders):
        if (now - order["created"]) < 900:
            continue
        t = tickers.get(order["symbol"])
        if not t:
            continue
        if order.get("requotes", 0) >= 2:
            paper.cancel_order(order["id"])
            await notify(f"❌ ОРДЕР СНЯТ · {fmt_sym(order['symbol'])}\nБез исполнения после 3 попыток.")
            continue
        candles = await market_data.get_kline(order["symbol"], "15", 60)
        a = atr(candles) if candles else 0
        if a <= 0:
            continue
        paper.cancel_order(order["id"])
        order["price"] = t["last"] * 0.998
        order["tp"] = order["price"] + 2.0 * a
        order["sl"] = order["price"] - 1.2 * a
        order["created"] = now
        order["requotes"] = order.get("requotes", 0) + 1
        paper.orders.append(order)
        paper.save()
        await notify(
            f"🔁 ОРДЕР ПЕРВЫСТАВЛЕН · {fmt_sym(order['symbol'])}\n"
            f"📥 Новая цена: {fmt_price(order['price'])} (попытка {order['requotes'] + 1})"
        )

    # 6. Сканирование и покупки / ротация
    candidates = []
    if regime != "bear":
        candidates = await scan(regime, tickers)
    else:
        logger.info("Медвежий рынок — новые покупки отключены (умный риск)")

    equity = paper.equity(tickers)

    if paper.usdt < 10 and candidates and paper.positions:
        best = candidates[0]
        weakest_sym, weakest_pos = min(paper.positions.items(), key=lambda kv: kv[1].get("score", 0))
        t = tickers.get(weakest_sym)
        if t:
            pnl_pct = (t["last"] - weakest_pos["avg"]) / weakest_pos["avg"] * 100 if weakest_pos["avg"] else 0
            if pnl_pct >= 1.0 and best["score"] >= weakest_pos.get("score", 0) + 1:
                paper._sell(weakest_sym, t["last"], "РОТАЦИЯ 🔄")
                await notify(
                    f"🔄 РОТАЦИЯ · продан {fmt_sym(weakest_sym)} ({fmt_pct(pnl_pct)})\n"
                    f"Сигнал {fmt_sym(best['symbol'])} сильнее (⭐ {best['score']})"
                )

    for cand in candidates:
        if paper.usdt < 10:
            break
        sym = cand["symbol"]
        if sym in paper.positions or any(o["symbol"] == sym for o in paper.orders):
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
        paper.place_limit_buy(sym, qty, entry, tp=tp, sl=sl, score=cand["score"])
        tp_pct = (tp - entry) / entry * 100
        sl_pct = (sl - entry) / entry * 100
        await notify(
            f"🎯 ВЫСТАВЛЕНА ПОКУПКА · {fmt_sym(sym)}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 Сумма: {fmt_usdt(size)} USDT\n"
            f"📥 Вход: {fmt_price(entry)}\n"
            f"🎯 TP: {fmt_price(tp)} ({fmt_pct(tp_pct)})\n"
            f"🛡 SL: {fmt_price(sl)} ({fmt_pct(sl_pct)})\n"
            f"⭐ Сигнал: {cand['score']}\n"
            f"🧠 Причина: {'; '.join(cand['reasons'][:4])}"
        )

    logger.info("=== CYCLE END ===")
