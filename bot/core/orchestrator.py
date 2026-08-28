import time

from loguru import logger

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.scanner import get_regime, scan, score_symbol, threshold
from bot.strategy.sizing import buy_size, portfolio_limits
from bot.strategy.indicators import atr, ema
from bot.core.state import bot_state
from bot.news.cmc import get_coin_name
from bot.news.rss_news import fetch_news_cache, check_sentiment
from bot.strategy.learner import learner
from bot.utils.format import fmt_price, fmt_pct, fmt_sym

CYCLE_SECONDS = 300
FEE_PCT = 0.10
MIN_TP_PCT = 0.60
MIN_SL_PCT = 0.35
MAX_SL_PCT = 3.0
MIN_RR = 1.5
SAT_MAX_SL_PCT = 5.0
MIN_RR_SAT = 2.0
MIN_EARLY_EXIT_PCT = 1.0
_notify_cb = None
_reconciled = False


def usd(x):
    return f"${x:,.2f}"


def pnl_emoji(x):
    return "🟢" if x > 0.05 else ("🔴" if x < -0.05 else "🟡")


def pair_html(sym, sector, kind_tag="🏛"):
    """Единый формат пары во всех сообщениях."""
    return f"{kind_tag} <b>{sym}</b> · <i>{sector}</i>"


def kind_tag_of(d):
    """🛰/🏛 из словаря позиции/ордера/результата."""
    return "🛰" if d.get("kind") == "satellite" else "🏛"


def funding_line(x):
    """Строка про накопления — только если реально пополнились."""
    return f"\n🏦 в накопления {usd(x)}" if x > 0 else ""


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
    logger.info("=== RECONCILE START ===")
    prices = await market_data.get_tickers()
    if not prices:
        logger.error("Reconcile: нет цен, пропуск")
        return
    actions = []

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

        if pos.get("tp1_done"):
            if pos.get("sl", 0) < entry:
                pos["sl"] = round(entry, 10)
                fixed.append(f"SL {fmt_price(pos['sl'])} (безубыток)")
        else:
            sl = pos.get("sl", 0)
            sl_dist = (entry - sl) / entry * 100 if sl else 0
            max_sl = SAT_MAX_SL_PCT if pos.get("kind") == "satellite" else MAX_SL_PCT
            if not sl or (sl < entry and (sl_dist > max_sl or sl_dist < MIN_SL_PCT)):
                d = max(min(1.2 * a / entry * 100, max_sl), MIN_SL_PCT)
                pos["sl"] = round(entry * (1 - d / 100), 10)
                fixed.append(f"SL {fmt_price(pos['sl'])}")
        if fixed:
            actions.append(f"{pair_html(sym[:-4], pos.get('sector') or 'Other', kind_tag_of(pos))} · {' · '.join(fixed)}")
    paper.save()

    regime, _ = await get_regime()
    thr = threshold(regime)
    for order in list(paper.orders):
        sym = order["symbol"]
        t = prices.get(sym)
        if not t:
            paper.cancel_order(order["id"])
            actions.append(f"{pair_html(sym[:-4], order.get('sector') or 'Other', kind_tag_of(order))} · ордер снят (нет данных)")
            continue
        candles = await market_data.get_kline(sym, "15", 120)
        if len(candles) < 60:
            continue
        a = atr(candles)
        score, _, _ = score_symbol(candles, t, regime)
        if score < thr - 1:
            paper.cancel_order(order["id"])
            actions.append(f"{pair_html(sym[:-4], order.get('sector') or 'Other', kind_tag_of(order))} · ордер снят · ⭐ {score:.1f} ниже {thr:g}")
        elif a > 0:
            order["price"] = t["last"] * 0.998
            order["tp"] = max(order["price"] + 2.0 * a, order["price"] * (1 + MIN_TP_PCT / 100))
            order["sl"] = min(order["price"] - 1.2 * a, order["price"] * (1 - MIN_SL_PCT / 100))
            order["created"] = int(time.time())
            order["requotes"] = 0
            actions.append(f"{pair_html(sym[:-4], order.get('sector') or 'Other', kind_tag_of(order))} · ордер перевыставлен · ⭐ {score:.1f}")
    paper.save()

    for line in actions:
        logger.info(f"RECONCILE: {line}")
    logger.info(f"=== RECONCILE END ({len(actions)} действий) ===")
    if actions:
        await notify("🧩 <b>Переоценка после старта</b>\n" + "\n".join(f"• {a}" for a in actions[:10]))


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

    metrics = paper.get_metrics(tickers)
    new_thr_adj = learner.update_threshold(
        metrics["profit_factor"], metrics["max_drawdown_pct"], metrics["total_trades"]
    )
    mode, _ = learner.risk_mode(
        metrics["profit_factor"], metrics["max_drawdown_pct"], metrics["total_trades"]
    )
    pf = metrics["profit_factor"]
    pf_txt = "∞" if pf == float("inf") else (f"{pf:.2f}" if pf is not None else "—")
    logger.info(f"METRICS: PF={pf_txt} | DD={metrics['max_drawdown_pct']:.1f}% | "
                f"mode={mode} | thr_adj={new_thr_adj:+.1f}")

    news_items = await fetch_news_cache()

    # 1. Исполнения покупок: 🛒 Покупка (с процентами TP/SL)
    for f in paper.check_fills(tickers):
        pos = paper.positions.get(f["symbol"])
        if pos is not None:
            pos["kind"] = f.get("kind", "core")
            pos["sector"] = f.get("sector", "Other")
            paper.save()
        kind_tag = kind_tag_of(f)
        sector = (pos.get("sector") if pos else None) or f.get("sector") or "Other"
        tp_pct = (f["tp"] - f["price"]) / f["price"] * 100
        sl_pct = (f["sl"] - f["price"]) / f["price"] * 100
        await notify(
            f"🛒 <b>Покупка</b> · {pair_html(f['symbol'][:-4], sector, kind_tag)}\n"
            f"💵 {usd(f['qty'] * f['price'])} · 📥 {fmt_price(f['price'])}\n"
            f"🎯 {fmt_price(f['tp'])} ({fmt_pct(tp_pct)}) · 🛡 {fmt_price(f['sl'])} ({fmt_pct(sl_pct)})"
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
                    f"🚨 <b>Экстренный выход</b> · {pair_html(ex['symbol'][:-4], ex.get('sector', 'Other'), kind_tag_of(ex))} · "
                    f"{pnl_emoji(ex['pnl_pct'])} {ex['pnl']:+.2f}% · {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}"
                    f"{funding_line(ex.get('transferred', 0))}"
                )
            await notify("🚨 <b>Риск-менеджмент</b>: резкий дамп рынка — всё в $.")
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
        pos["max_price"] = max(pos.get("max_price", 0.0), last)
        pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
        e21, e50 = ema(closes, 21)[-1], ema(closes, 50)[-1]
        score_pos, _, _ = score_symbol(candles, t, regime)
        thr = threshold(regime)
        trend_broken = last < e50 and e21 < e50

        # 4.0 НОВОСТНАЯ ПРОВЕРКА ПОЗИЦИИ
        base = sym[:-4]
        name = await get_coin_name(base)
        neg, pos_news, _, _ = check_sentiment(news_items, [base, name])
        if neg > 0 and neg > pos_news:
            if pnl_pct >= MIN_EARLY_EXIT_PCT:
                ex = paper._sell(sym, last, "НОВОСТИ ⚠️")
                await notify(
                    f"💸 <b>Продажа</b> · {pair_html(sym[:-4], ex.get('sector', 'Other'), kind_tag_of(ex))} · новостной выход ⚠️ {neg}\n"
                    f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}"
                    f"{funding_line(ex.get('transferred', 0))}"
                )
                continue
            elif pnl_pct <= 0:
                ex = paper._sell(sym, last, "НОВОСТИ 🛑")
                await notify(
                    f"💸 <b>Продажа</b> · {pair_html(sym[:-4], ex.get('sector', 'Other'), kind_tag_of(ex))} · новостная резка 🛑 {neg}\n"
                    f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}"
                    f"{funding_line(ex.get('transferred', 0))}"
                )
                continue

        # 4а. ЧАСТИЧНЫЙ TP
        if not pos.get("tp1_done") and last >= pos["tp"]:
            half = pos["qty"] / 2
            ex = paper.sell_partial(sym, half, pos["tp"], "TP1 🎯")
            pos["tp1_done"] = True
            pos["sl"] = max(pos["sl"], pos["avg"])
            pos["tp"] = round(pos["tp"] + 1.5 * a, 10)
            paper.save()
            await notify(
                f"🎯 <b>TP1</b> · {pair_html(sym[:-4], ex.get('sector', 'Other'), kind_tag_of(ex))} · 50%\n"
                f"📊 {fmt_price(ex['price'])} · 🔥 {fmt_pct(ex['pnl_pct'])} · 💵 прибыль {usd(ex['pnl'])}"
                f"{funding_line(ex.get('transferred', 0))}\n"
                f"остаток бежит · 🎯 {fmt_price(pos['tp'])} · 🛡 {fmt_price(pos['sl'])} (безубыток)"
            )
            continue

        # 4б. ИНВАЛИДАЦИЯ
        if trend_broken or score_pos <= thr - 2:
            if pnl_pct >= MIN_EARLY_EXIT_PCT:
                ex = paper._sell(sym, last, "СИГНАЛ ИСЯК 📉")
                await notify(
                    f"💸 <b>Продажа</b> · {pair_html(sym[:-4], ex.get('sector', 'Other'), kind_tag_of(ex))} · сигнал ослаб 📉\n"
                    f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}"
                    f"{funding_line(ex.get('transferred', 0))}"
                )
                continue
            if trend_broken and score_pos <= thr - 1 and pnl_pct <= -0.5:
                ex = paper._sell(sym, last, "ИНВАЛИДАЦИЯ 🛑")
                await notify(
                    f"💸 <b>Продажа</b> · {pair_html(sym[:-4], ex.get('sector', 'Other'), kind_tag_of(ex))} · резка убытка 🛑\n"
                    f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}"
                    f"{funding_line(ex.get('transferred', 0))}"
                )
                continue

        # 4в. РАННЕР
        new_sl = None
        if pos.get("tp1_done") and last > e21 > e50 and score_pos >= thr and last >= pos["tp"] - 0.3 * a:
            new_tp = max(pos["tp"], last + 1.5 * a)
            if new_tp > pos["tp"]:
                pos["tp"] = round(new_tp, 10)
                new_sl = max(new_sl or pos["sl"], pos["avg"] + 0.5 * a)
                await notify(
                    f"🎯 <b>TP поднят</b> (раннер) · {pair_html(sym[:-4], pos.get('sector') or 'Other', kind_tag_of(pos))}\n"
                    f"🎯 {fmt_price(pos['tp'])} · 🛡 {fmt_price(pos['sl'])}"
                )

        # 4г. Трейлинг SL — только вверх
        if last >= pos["avg"] + 1.0 * a:
            new_sl = max(new_sl or pos["sl"], pos["avg"])
        if last >= pos["avg"] + 2.0 * a:
            new_sl = max(new_sl or pos["sl"], last - 0.8 * a)
        if new_sl:
            new_sl_r = round(new_sl, 10)
            if new_sl_r > pos["sl"]:
                pos["sl"] = new_sl_r
                pos["max_sl"] = max(pos.get("max_sl", 0), pos["sl"])
                logger.info(f"SL поднят {sym} -> {pos['sl']}")
        paper.save()

    # 5. Выходы остатка по TP/SL: 💸 Продажа с ценой, типом и накоплениями
    for ex in paper.check_exits(tickers):
        runner_txt = ""
        if ex.get("runner_bonus", 0) > 5:
            runner_txt = f"\n🏃 пробежка +{ex['runner_bonus']:.1f}% выше TP1"
        ind = "🔥" if ex.get("exit_type") == "TP1_RUN" else pnl_emoji(ex["pnl_pct"])
        await notify(
            f"💸 <b>Продажа</b> · {pair_html(ex['symbol'][:-4], ex.get('sector', 'Other'), kind_tag_of(ex))} · {ex['reason']}\n"
            f"{ind} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}{runner_txt}"
            f"{funding_line(ex.get('transferred', 0))}"
        )

    # 6. Неисполненные ордера (с типом и сектором)
    now = int(time.time())
    for order in list(paper.orders):
        t = tickers.get(order["symbol"])
        if not t:
            continue

        base = order["symbol"][:-4]
        o_pair = pair_html(base, order.get("sector") or "Other", kind_tag_of(order))
        name = await get_coin_name(base)
        neg, pos_news, _, _ = check_sentiment(news_items, [base, name])
        if neg > 0 and neg > pos_news:
            paper.cancel_order(order["id"])
            await notify(f"⚠️ <b>Ордер снят</b> · {o_pair} · негатив {neg}")
            continue

        if (now - order["created"]) < 900:
            continue
        if order.get("requotes", 0) >= 2:
            paper.cancel_order(order["id"])
            await notify(f"❌ <b>Ордер снят</b> · {o_pair} · 3 попытки без исполнения")
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
            f"🔁 <b>Ордер перевыставлен</b> · {o_pair}\n"
            f"📥 {fmt_price(order['price'])} · попытка {order['requotes'] + 1}"
        )

    # 7. Сканирование и покупки / ротация
    candidates = []
    if regime != "bear":
        candidates = await scan(regime, tickers)
    else:
        logger.info("Медвежий рынок — новые покупки отключены (умный риск)")

    equity = paper.equity(tickers)
    sec_lim, other_lim = portfolio_limits(equity)
    sat_limit = learner.satellite_limit()
    sat_size = learner.satellite_size_pct()
    logger.info(f"PORTFOLIO LIMITS: equity={equity:.0f} | "
                f"лимит на сектор={sec_lim} | лимит Other={other_lim} | "
                f"лимит сателлитов={sat_limit:.0f}% · размер сателлита={sat_size:.1f}% | "
                f"всего позиций: без лимита")

    if paper.usdt < 10 and candidates and paper.positions:
        best = candidates[0]
        weakest_sym, weakest_pos = min(paper.positions.items(), key=lambda kv: kv[1].get("score", 0))
        t = tickers.get(weakest_sym)
        if t:
            pnl_pct = (t["last"] - weakest_pos["avg"]) / weakest_pos["avg"] * 100 if weakest_pos["avg"] else 0
            if pnl_pct >= 1.0 and best["score"] >= weakest_pos.get("score", 0) + 1:
                ex = paper._sell(weakest_sym, t["last"], "РОТАЦИЯ 🔄")
                await notify(
                    f"🔄 <b>Ротация</b> · <b>{weakest_sym[:-4]}</b> → <b>{best['symbol'][:-4]}</b>\n"
                    f"{pnl_emoji(pnl_pct)} {fmt_pct(pnl_pct)} · ⭐ {best['score']:.1f}"
                    f"{funding_line(ex.get('transferred', 0))}"
                )

    for cand in candidates:
        if paper.usdt < 10:
            break
        sym = cand["symbol"]
        if sym in paper.positions or any(o["symbol"] == sym for o in paper.orders):
            continue

        kind = cand.get("kind", "core")
        sector = cand.get("sector", "Other")

        lim = other_lim if sector == "Other" else sec_lim
        sector_count = sum(
            1 for p in paper.positions.values() if (p.get("sector") or "Other") == sector
        ) + sum(1 for o in paper.orders if (o.get("sector") or "Other") == sector)

        if sector_count >= lim:
            sector_positions = [
                (s, p) for s, p in paper.positions.items()
                if (p.get("sector") or "Other") == sector
            ]
            rotated = False

            if sector == "Other" and sector_positions:
                other_symbols = [s[:-4] for s, _ in sector_positions]
                logger.info(f"Other позиции: {', '.join(other_symbols)}")

            if sector_positions:
                weakest_sym, weakest_pos = min(
                    sector_positions, key=lambda kv: kv[1].get("score", 0)
                )
                weakest_score = weakest_pos.get("score", 0)
                t_weak = tickers.get(weakest_sym)
                if t_weak:
                    weak_pnl = (t_weak["last"] - weakest_pos["avg"]) / weakest_pos["avg"] * 100

                    if sector == "Other":
                        can_rotate = (
                            cand["score"] >= weakest_score + 1.0
                            and not weakest_pos.get("tp1_done")
                            and weak_pnl >= -2.0
                        )
                    else:
                        can_rotate = (
                            cand["score"] >= weakest_score + 1.5
                            and not weakest_pos.get("tp1_done")
                            and weak_pnl >= -0.5
                            and weak_pnl < 2.0
                        )

                    if can_rotate:
                        ex = paper._sell(weakest_sym, t_weak["last"], "РОТАЦИЯ СЕКТОРА 🔄")
                        sector_count -= 1
                        await notify(
                            f"🔄 <b>Ротация сектора</b> {sector} · <b>{weakest_sym[:-4]}</b> → <b>{sym[:-4]}</b>\n"
                            f"{pnl_emoji(weak_pnl)} {fmt_pct(weak_pnl)} · ⭐ {weakest_score:.1f} → {cand['score']:.1f}\n"
                            f"💵 {usd(ex['pnl'])}{funding_line(ex.get('transferred', 0))}"
                        )
                        logger.info(f"{sym}: ротация сектора — продан {weakest_sym} "
                                    f"(score {weakest_score:.1f}, pnl {weak_pnl:+.2f}%) "
                                    f"для {sym} (score {cand['score']:.1f})")
                        rotated = True
            if sector_count >= lim:
                reason = ("слабых кандидатов для ротации нет" if not rotated
                          else "после ротации лимит всё ещё заполнен")
                logger.info(f"{sym}: пропущен — сектор {sector} переполнен ({sector_count}/{lim}, {reason})")
                continue

        # ЛИМИТ САТЕЛЛИТОВ — адаптивный (10–30%)
        if kind == "satellite":
            sat_exposure = sum(
                p["qty"] * tickers.get(s, {}).get("last", 0)
                for s, p in paper.positions.items() if p.get("kind") == "satellite"
            ) + sum(
                o["qty"] * o["price"] for o in paper.orders if o.get("kind") == "satellite"
            )
            if sat_exposure >= equity * sat_limit / 100:
                logger.info(f"{sym}: пропущен — лимит сателлитов исчерпан ({sat_limit:.0f}%)")
                continue

        entry = cand["last"] * 0.998
        a = cand["atr"]
        if a <= 0:
            continue

        if kind == "satellite":
            sl_dist_pct = max(min(1.5 * a / entry * 100, SAT_MAX_SL_PCT), 2.0)
            tp_dist_pct = max(min(2.5 * a / entry * 100, 12.0), sl_dist_pct * MIN_RR_SAT)
            sl = entry * (1 - sl_dist_pct / 100)
            tp = entry * (1 + tp_dist_pct / 100)
            min_rr = MIN_RR_SAT
        else:
            sl = entry - 1.2 * a
            tp = entry + 2.0 * a
            tp = max(tp, entry * (1 + MIN_TP_PCT / 100))
            sl = min(sl, entry * (1 - MIN_SL_PCT / 100))
            sl_dist_pct = (entry - sl) / entry * 100
            min_rr = MIN_RR
            if sl_dist_pct > MAX_SL_PCT:
                logger.info(f"{sym}: пропущен — SL слишком далеко ({sl_dist_pct:.1f}%)")
                continue

        sl_dist = (entry - sl) / entry * 100
        rr = (tp - entry) / (entry - sl) if entry > sl else 0
        if tp <= entry or sl >= entry or rr < min_rr:
            continue

        size = buy_size(equity, cand["score"], cand["liquidity"], paper.usdt,
                        sl_dist, kind=kind, realized=paper.realized,
                        sat_size_pct=sat_size)
        if size < 5:
            continue

        pending_amount = sum(o["qty"] * o["price"] for o in paper.orders)
        if pending_amount + size > paper.usdt:
            rotated_order = False
            while paper.orders and pending_amount + size > paper.usdt:
                worst = min(paper.orders, key=lambda o: o.get("score", 0))
                worst_score = worst.get("score", 0)
                if cand["score"] >= worst_score + 1.0:
                    paper.cancel_order(worst["id"])
                    pending_amount -= worst["qty"] * worst["price"]
                    await notify(
                        f"🔄 <b>Ротация ордера</b> · <b>{worst['symbol'][:-4]}</b> снят\n"
                        f"Место для <b>{sym[:-4]}</b> · ⭐ {worst_score:.1f} → {cand['score']:.1f}"
                    )
                    logger.info(f"{sym}: ротация — снят {worst['symbol']} "
                                f"(score {worst_score:.1f}) для {sym} (score {cand['score']:.1f})")
                    rotated_order = True
                else:
                    break
            if pending_amount + size > paper.usdt:
                reason = "не хватает свободных средств" if not rotated_order else "новый сигнал слабее худшего"
                logger.info(f"{sym}: пропущен — {reason} "
                            f"(pending {pending_amount:.0f} + {size:.0f} > free {paper.usdt:.0f})")
                continue

        qty = size / entry
        order = paper.place_limit_buy(sym, qty, entry, tp=tp, sl=sl,
                                      score=cand["score"],
                                      reason_keys=cand.get("reason_keys", []))
        order["kind"] = kind
        order["sector"] = sector
        paper.save()
        tp_pct = (tp - entry) / entry * 100
        sl_pct = (sl - entry) / entry * 100
        kind_tag = "🛰" if kind == "satellite" else "🏛"
        new_tag = "· 🆕 " if cand.get("is_new") else ""
        await notify(
            f"📋 <b>Ордер</b> {new_tag}· {pair_html(sym[:-4], sector, kind_tag)}\n"
            f"💵 {usd(size)} · 📥 {fmt_price(entry)}\n"
            f"🎯 {fmt_price(tp)} ({fmt_pct(tp_pct)}) · 🛡 {fmt_price(sl)} ({fmt_pct(sl_pct)})\n"
            f"⭐ {cand['score']:.1f} · 🧠 {'; '.join(cand['reasons'][:3])}"
        )

    logger.info("=== CYCLE END ===")
