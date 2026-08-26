import time

from loguru import logger

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.scanner import get_regime, scan, score_symbol, threshold
from bot.strategy.sizing import buy_size
from bot.strategy.indicators import atr, ema
from bot.core.state import bot_state
from bot.utils.format import fmt_price, fmt_usdt, fmt_pct, fmt_sym

CYCLE_SECONDS = 300
FEE_PCT = 0.10
MIN_TP_PCT = 0.60
MIN_SL_PCT = 0.35
MAX_SL_PCT = 3.0
MIN_RR = 1.5
MIN_EARLY_EXIT_PCT = 1.0
_notify_cb = None
_reconciled = False


def set_notifier(cb):
    global _notify_cb
    _notify_cb = cb


async def notify(text):
    if _notify_cb:
        try:
            await _notify_cb(text)
        except Exception as e:
            logger.error(f"notify error: {e}")


# ==================== СВЕРКА СОСТОЯНИЯ ПРИ СТАРТЕ ====================
async def startup_reconciliation():
    """После деплоя/рестарта: привести позиции и ордера к ТЕКУЩЕЙ логике.
    Для реальной биржи сюда добавится сканирование балансов/ордеров на бирже."""
    logger.info("=== RECONCILE START ===")
    prices = await market_data.get_tickers()
    if not prices:
        logger.error("Reconcile: нет цен, пропуск")
        return
    actions = []

    # 1. Позиции: пересчитать TP/SL по актуальным правилам (если они пропали или устарели)
    for sym, pos in list(paper.positions.items()):
        entry = pos.get("avg", 0)
        if not entry:
            continue
        candles = await market_data.get_kline(sym, "15", 120)
        if len(candles) < 60:
            continue
        a = atr(candles)
        if a <= 0:
            continue
        fixed = []
        tp_dist = (pos.get("tp", 0) - entry) / entry * 100 if pos.get("tp") else 0
        if not pos.get("tp1_done") and (not pos.get("tp") or pos["tp"] <= entry or tp_dist < MIN_TP_PCT):
            d = max(min(2.0 * a / entry * 100, MAX_SL_PCT * 2), MIN_TP_PCT)
            pos["tp"] = round(entry * (1 + d / 100), 10)
            fixed.append(f"TP {fmt_price(pos['tp'])}")
        sl_dist = (entry - pos.get("sl", 0)) / entry * 100 if pos.get("sl") else 0
        if not pos.get("sl") or pos["sl"] >= entry or sl_dist > MAX_SL_PCT or sl_dist < MIN_SL_PCT:
            d = max(min(1.2 * a / entry * 100, MAX_SL_PCT), MIN_SL_PCT)
            pos["sl"] = round(entry * (1 - d / 100), 10)
            fixed.append(f"SL {fmt_price(pos['sl'])}")
        if fixed:
            actions.append(f"{sym}: {' и '.join(fixed)} пересчитаны по новой логике")
    paper.save()

    # 2. Ордера: проверить сигнал текущим скорингом; протухшие — снять, живые — освежить
    regime, _ = await get_regime()
    thr = threshold(regime)
    for order in list(paper.orders):
        sym = order["symbol"]
        t = prices.get(sym)
        if not t:
            paper.cancel_order(order["id"])
            actions.append(f"{sym}: ордер снят (нет данных)")
            continue
        candles = await market_data.get_kline(sym, "15", 120)
        if len(candles) < 60:
            continue
        a = atr(candles)
        score, _, _ = score_symbol(candles, t, regime)
        if score < thr - 1:
            paper.cancel_order(order["id"])
            actions.append(f"{sym}: ордер снят (score {score:.1f} ниже порога {thr})")
        elif a > 0:
            order["price"] = t["last"] * 0.998
            order["tp"] = max(order["price"] + 2.0 * a, order["price"] * (1 + MIN_TP_PCT / 100))
            order["sl"] = min(order["price"] - 1.2 * a, order["price"] * (1 - MIN_SL_PCT / 100))
            order["created"] = int(time.time())
            order["requotes"] = 0
            actions.append(f"{sym}: ордер перевыставлен по новой логике (score {score:.1f})")
    paper.save()

    for line in actions:
        logger.info(f"RECONCILE: {line}")
    logger.info(f"=== RECONCILE END ({len(actions)} действий) ===")
    if actions:
        await notify("🧩 ПЕРЕОЦЕНКА ПОСЛЕ СТАРТА:\n" + "\n".join(actions[:10]))


async def maybe_reconcile():
    global _reconciled
    if _reconciled:
        return
    _reconciled = True
    try:
        await startup_reconciliation()
    except Exception as e:
        logger.exception(f"Reconcile error: {e}")


# ==================== ТОРГОВЫЙ ЦИКЛ ====================
async def run_cycle():
    await maybe_reconcile()

    if bot_state.paused or not bot_state.trading_enabled:
        logger.info("Цикл пропущен: торговля на паузе или остановлена")
        return

    logger.info("=== CYCLE START ===")
    tickers = await market_data.get_tickers()
    if not tickers:
        logger.error("Нет тикеров — цикл пропущен")
        return

    # 1. Исполнения покупок
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

    # 4. УПРАВЛЕНИЕ ПОЗИЦИЯМИ
    for sym, pos in list(paper.positions.items()):
        t = tickers.get(sym)
        if not t:
            continue
        candles = await market_data.get_kline(sym, "15", 60)
        if len(candles) < 30:
            continue
        closes = [c["close"] for c in candles]
        a = atr(candles)
        if a <= 0:
            continue
        last = t["last"]
        pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
        e21, e50 = ema(closes, 21)[-1], ema(closes, 50)[-1]
        score_pos, _, _ = score_symbol(candles, t, regime)
        thr = threshold(regime)
        trend_broken = last < e50 and e21 < e50

        # 4а. ЧАСТИЧНЫЙ TP: первый таргет -> продаём 50%, остаток бежит
        if not pos.get("tp1_done") and last >= pos["tp"]:
            half = pos["qty"] / 2
            ex = paper.sell_partial(sym, half, pos["tp"], "TP1 🎯")
            pos["tp1_done"] = True
            pos["sl"] = max(pos["sl"], pos["avg"])
            pos["tp"] = round(pos["tp"] + 1.5 * a, 10)
            paper.save()
            await notify(
                f"🎯 ЧАСТИЧНЫЙ TP · {fmt_sym(sym)}\n"
                f"Продано 50% по {fmt_price(ex['price'])} ({fmt_pct(ex['pnl_pct'])}, {ex['pnl']:+.2f} USDT)\n"
                f"Остаток бежит дальше: 🎯 {fmt_price(pos['tp'])} | 🛡 {fmt_price(pos['sl'])}"
            )
            continue

        # 4б. ИНВАЛИДАЦИЯ: тренд сломан / сигнал умер
        if trend_broken or score_pos <= thr - 2:
            if pnl_pct >= MIN_EARLY_EXIT_PCT:
                ex = paper._sell(sym, last, "СИГНАЛ ИСЯК 📉")
                await notify(
                    f"📉 РАННИЙ ВЫХОД · {fmt_sym(sym)}\n"
                    f"Сигнал ослаб (score {score_pos:.1f} при пороге {thr}) — "
                    f"фиксируем {fmt_pct(ex['pnl_pct'])} ({ex['pnl']:+.2f} USDT)"
                )
                continue
            if trend_broken and score_pos <= thr - 1 and pnl_pct <= -0.5:
                ex = paper._sell(sym, last, "ИНВАЛИДАЦИЯ 🛑")
                await notify(
                    f"🛑 РЕЗКА УБЫТКА · {fmt_sym(sym)}\n"
                    f"Тренд сломан, сигнал мёртв (score {score_pos:.1f}) — "
                    f"режем {fmt_pct(ex['pnl_pct'])} ({ex['pnl']:+.2f} USDT), не ждём SL"
                )
                continue

        # 4в. РАННЕР: тренд силён у нового TP -> двигаем TP вверх
        new_sl = None
        if pos.get("tp1_done") and last > e21 > e50 and score_pos >= thr and last >= pos["tp"] - 0.3 * a:
            new_tp = max(pos["tp"], last + 1.5 * a)
            if new_tp > pos["tp"]:
                pos["tp"] = round(new_tp, 10)
                new_sl = max(new_sl or pos["sl"], pos["avg"] + 0.5 * a)
                await notify(
                    f"🎯 TP ПОДНЯТ (раннер) · {fmt_sym(sym)}\n"
                    f"🎯 {fmt_price(pos['tp'])} | 🛡 {fmt_price(pos['sl'])}"
                )

        # 4г. Трейлинг SL — только вверх
        if last >= pos["avg"] + 1.0 * a:
            new_sl = max(new_sl or pos["sl"], pos["avg"])
        if last >= pos["avg"] + 2.0 * a:
            new_sl = max(new_sl or pos["sl"], last - 0.8 * a)
        if new_sl and new_sl > pos["sl"]:
            pos["sl"] = round(new_sl, 10)
            pos["max_sl"] = max(pos.get("max_sl", 0), pos["sl"])
            logger.info(f"SL поднят {sym} -> {pos['sl']}")
        paper.save()

    # 5. Выходы остатка по TP/SL
    for ex in paper.check_exits(tickers):
        await notify(
            f"📤 ПРОДАЖА · {fmt_sym(ex['symbol'])} · {ex['reason']}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 Цена: {fmt_price(ex['price'])} ({fmt_pct(ex['pnl_pct'])})\n"
            f"💵 PnL: {ex['pnl']:+.2f} USDT\n"
            f"🏦 В Funding: {fmt_usdt(ex['transferred'])} USDT"
        )

    # 6. Неисполненные ордера: перевыставление
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
        order["tp"] = max(order["price"] + 2.0 * a, order["price"] * (1 + MIN_TP_PCT / 100))
        order["sl"] = min(order["price"] - 1.2 * a, order["price"] * (1 - MIN_SL_PCT / 100))
        order["created"] = now
        order["requotes"] = order.get("requotes", 0) + 1
        paper.orders.append(order)
        paper.save()
        await notify(
            f"🔁 ОРДЕР ПЕРВЫСТАВЛЕН · {fmt_sym(order['symbol'])}\n"
            f"📥 Новая цена: {fmt_price(order['price'])} (попытка {order['requotes'] + 1})"
        )

    # 7. Сканирование и покупки / ротация
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
                    f"Сигнал {fmt_sym(best['symbol'])} сильнее (⭐ {best['score']:.1f})"
                )

    for cand in candidates:
        if paper.usdt < 10:
            break
        sym = cand["symbol"]
        if sym in paper.positions or any(o["symbol"] == sym for o in paper.orders):
            continue
        entry = cand["last"] * 0.998
        a = cand["atr"]
        if a <= 0:
            continue
        sl, tp = entry - 1.2 * a, entry + 2.0 * a
        tp = max(tp, entry * (1 + MIN_TP_PCT / 100))
        sl = min(sl, entry * (1 - MIN_SL_PCT / 100))
        sl_dist = (entry - sl) / entry * 100
        if sl_dist > MAX_SL_PCT:
            logger.info(f"{sym}: пропущен — SL слишком далеко ({sl_dist:.1f}%)")
            continue
        rr = (tp - entry) / (entry - sl) if entry > sl else 0
        if tp <= entry or sl >= entry or rr < MIN_RR:
            continue
        size = buy_size(equity, cand["score"], cand["liquidity"], paper.usdt, sl_dist)
        if size < 5:
            continue
        qty = size / entry
        paper.place_limit_buy(sym, qty, entry, tp=tp, sl=sl,
                              score=cand["score"], reason_keys=cand.get("reason_keys", []))
        tp_pct = (tp - entry) / entry * 100
        sl_pct = (sl - entry) / entry * 100
        await notify(
            f"🎯 ВЫСТАВЛЕНА ПОКУПКА · {fmt_sym(sym)}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💵 Сумма: {fmt_usdt(size)} USDT\n"
            f"📥 Вход: {fmt_price(entry)}\n"
            f"🎯 TP: {fmt_price(tp)} ({fmt_pct(tp_pct)})\n"
            f"🛡 SL: {fmt_price(sl)} ({fmt_pct(sl_pct)})\n"
            f"⭐ Сигнал: {cand['score']:.1f} | 🔗 BTC: {cand.get('corr', 0):.2f}\n"
            f"🧠 Причина: {'; '.join(cand['reasons'][:4])}"
        )

    logger.info("=== CYCLE END ===")
