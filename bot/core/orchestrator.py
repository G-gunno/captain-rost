import time

from loguru import logger

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.scanner import get_regime, scan, score_symbol, threshold, live_score
from bot.strategy.sizing import buy_size, portfolio_limits
from bot.strategy.indicators import atr, ema
from bot.core.state import bot_state
from bot.news.cmc import get_coin_name, TIER_EMOJI
from bot.news.rss_news import fetch_news_cache, check_sentiment
from bot.strategy.learner import learner
from bot.strategy.shadow import shadow
from bot.utils.format import fmt_price, fmt_pct, fmt_sym

CYCLE_SECONDS = 60
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
_last_mode = None
_last_regime = None
_fomo_cooldowns = {}  # память для отмененных ордеров: {symbol: expire_timestamp}

# Буфер для объединения уведомлений в один дайджест за цикл
_notification_buffer = []


def usd(x):
    return f"${x:,.2f}"


def pnl_emoji(x):
    return "🟢" if x > 0.05 else ("🔴" if x < -0.05 else "🟡")


def pair_html(sym, sector, kind_tag="🏛", tier=None):
    em = TIER_EMOJI.get(tier, "") if tier else ""
    return f"{kind_tag} <b>{sym}</b>{' ' + em if em else ''} · <i>{sector}</i>"


def kind_tag_of(d):
    return "🛰" if d.get("kind") == "satellite" else "🏛"


def funding_line(x):
    return f"\n🏦 в накопления {usd(x)}" if x > 0 else ""


def corr_txt(d):
    v = d.get("corr") if isinstance(d, dict) else None
    return f" · ₿ {v:.2f}" if v is not None else ""


def entry_offset(score, thr, regime, atr_pct, is_momentum=False):
    """Смещение входа: гибридная логика (ракета vs снайпер)."""
    hunt = shadow.hunt() 
    
    if is_momentum:
        return max(shadow.capture(), atr_pct / 100 * 0.1)
    
    surplus = score - thr
    if regime == "bear":
        return hunt * 1.5  
    elif regime == "neutral":
        return hunt * 1.2  
        
    # Бычий рынок (Снайпер)
    if surplus >= 3.0:
        return max(shadow.near(), -atr_pct / 100 * 0.1)  # Сильный сигнал - берем почти по рынку
    if surplus >= 1.5:
        return min(hunt * 0.5, -atr_pct / 100 * 0.3)  # Средний сигнал - берем половину отката
        
    return hunt  # Слабый сигнал — берем стандартный откат (без умножения на 1.5)


def set_notifier(cb):
    global _notify_cb
    _notify_cb = cb


async def notify(text, urgent=False):
    """Если urgent=True — отправляет мгновенно. Иначе — складывает в буфер цикла."""
    if urgent:
        if _notify_cb:
            try:
                await _notify_cb(text)
            except Exception as e:
                logger.error(f"urgent notify error: {e}")
    else:
        _notification_buffer.append(text)


# ==================== СВЕРКА СОСТОЯНИЯ ПРИ СТАРТЕ ====================
async def startup_reconciliation():
    logger.info("=== RECONCILE START ===")
    prices = await market_data.get_tickers()
    deriv_tickers = await market_data.get_derivatives_tickers()
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
            breakeven_price = entry * (1 + (FEE_PCT * 2) / 100)
            if pos.get("sl", 0) < breakeven_price:
                pos["sl"] = round(breakeven_price, 10)
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
            actions.append(f"{pair_html(sym[:-4], pos.get('sector') or 'Other', kind_tag_of(pos), pos.get('tier'))} · {' · '.join(fixed)}")
    paper.save()

    regime, _ = await get_regime()
    thr = threshold(regime)
    for order in list(paper.orders):
        sym = order["symbol"]
        t = prices.get(sym)
        if not t:
            paper.cancel_order(order["id"])
            actions.append(f"{pair_html(sym[:-4], order.get('sector') or 'Other', kind_tag_of(order), order.get('tier'))} · снят 🪫 (нет данных)")
            continue
        score, candles = await live_score(sym, t, regime, deriv_t=deriv_tickers.get(sym))
        if score is None:
            continue
        a = atr(candles)
        
        # 1. Сигнал полностью умер
        if score < thr - 2:
            paper.cancel_order(order["id"])
            actions.append(f"{pair_html(sym[:-4], order.get('sector') or 'Other', kind_tag_of(order), order.get('tier'))} · снят 🪫 · ⭐ {score:.1f} &lt; {thr - 2:g}")
            continue
            
        # 2. Сигнал ослаб (ниже порога входа)
        if score < thr:
            paper.cancel_order(order["id"])
            actions.append(f"{pair_html(sym[:-4], order.get('sector') or 'Other', kind_tag_of(order), order.get('tier'))} · снят 🪫 · ⭐ {score:.1f} &lt; {thr:g}")
            continue
            
        # 3. Сигнал актуален -> Перевыставляем
        if a > 0:
            atr_pct = a / t["last"] * 100
            is_mom = order.get("is_momentum", False)
            off = entry_offset(score, thr, regime, atr_pct, is_mom)
            
            ideal_price = t["last"] * (1 + off)
            old_price = order["price"]
            bid1 = t.get("bid1", t["last"]) # <-- Лучший Bid
            
            if not is_mom and t["last"] > old_price + 1.5 * a:
                paper.cancel_order(order["id"])
                actions.append(f"{pair_html(sym[:-4], order.get('sector') or 'Other', kind_tag_of(order), order.get('tier'))} · снят 🚀 (улетел)")
                continue

            if is_mom:
                order["price"] = ideal_price
                price_icon = "⬆️" if ideal_price > old_price else "⬇️"
                sl_dist = 0.6 * a
            else:
                # Снайпер всегда Maker (не выше лучшего бида)
                ideal_price = min(ideal_price, bid1)
                if ideal_price > old_price:
                    order["price"] = old_price
                    price_icon = "⏸"
                else:
                    order["price"] = ideal_price
                    price_icon = "⬇️"
                sl_dist = 1.2 * a
            
            order["tp"] = max(order["price"] + 2.0 * a, order["price"] * (1 + MIN_TP_PCT / 100))
            order["sl"] = min(order["price"] - sl_dist, order["price"] * (1 - MIN_SL_PCT / 100))
            order["created"] = int(time.time())
            order["requotes"] = 0
            actions.append(f"{pair_html(sym[:-4], order.get('sector') or 'Other', kind_tag_of(order), order.get('tier'))} · перевыставлен {price_icon} · ⭐ {score:.1f}")
    paper.save()

    for line in actions:
        logger.info(f"RECONCILE: {line}")
    logger.info(f"=== RECONCILE END ({len(actions)} действий) ===")
    if actions:
        await notify("🧩 <b>Переоценка после старта</b>\n" + "\n".join(f"• {a}" for a in actions[:10]), urgent=True)


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
    global _last_mode, _last_regime
    await maybe_reconcile()

    if bot_state.paused or not bot_state.trading_enabled:
        logger.info("Цикл пропущен: торговля на паузе или остановлена")
        return

    logger.info("=== CYCLE START ===")
    tickers = await market_data.get_tickers()
    deriv_tickers = await market_data.get_derivatives_tickers()
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
    
    if _last_mode is not None and mode != _last_mode:
        m_em = {"NORMAL": "🟢", "CAUTIOUS": "🟡", "STRICT": "🔴", "AGGRESSIVE": "🚀"}
        old_e, new_e = m_em.get(_last_mode, '⚪'), m_em.get(mode, '⚪')
        await notify(f"🎚 <b>Смена режима риска</b>\n{old_e} {_last_mode} ➡️ {new_e} <b>{mode}</b>")
    _last_mode = mode

    pf = metrics["profit_factor"]
    pf_txt = "∞" if pf == float("inf") else (f"{pf:.2f}" if pf is not None else "—")
    logger.info(f"METRICS: PF={pf_txt} | DD={metrics['max_drawdown_pct']:.1f}% | "
                f"mode={mode} | thr_adj={new_thr_adj:+.1f}")

    news_items = await fetch_news_cache()

    # 1. Исполнения покупок
    for f in paper.check_fills(tickers):
        pos = paper.positions.get(f["symbol"])
        if pos is not None:
            pos["kind"] = f.get("kind", "core")
            pos["sector"] = f.get("sector", "Other")
            pos["tier"] = f.get("tier")
            pos["regime_entry"] = f.get("regime")
            pos["corr"] = f.get("corr", 0.5)
            paper.save()
        kind_tag = kind_tag_of(f)
        sector = (pos.get("sector") if pos else None) or f.get("sector") or "Other"
        tier = (pos.get("tier") if pos else None) or f.get("tier")
        tp_pct = (f["tp"] - f["price"]) / f["price"] * 100
        sl_pct = (f["sl"] - f["price"]) / f["price"] * 100
        await notify(
            f"🛒 <b>Покупка</b> · {pair_html(f['symbol'][:-4], sector, kind_tag, tier)}\n"
            f"💵 {usd(f['qty'] * f['price'])} · 📥 {fmt_price(f['price'])}{corr_txt(f)}\n"
            f"🎯 {fmt_price(f['tp'])} ({fmt_pct(tp_pct)}) · 🛡 {fmt_price(f['sl'])} ({fmt_pct(sl_pct)})"
        )

    # 2. Оценка рынка
    regime, info = await get_regime()
    logger.info(f"Regime: {regime} | {info}")
    
    if _last_regime is not None and regime != _last_regime:
        r_em = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}
        r_tx = {"bull": "бычий", "neutral": "нейтральный", "bear": "медвежий"}
        old_str = f"{r_em.get(_last_regime, '⚪')} {r_tx.get(_last_regime, _last_regime)}"
        new_str = f"{r_em.get(regime, '⚪')} <b>{r_tx.get(regime, regime)}</b>"
        await notify(f"🧭 <b>Смена фазы рынка</b>\n{old_str} ➡️ {new_str}\n₿ {fmt_price(info.get('btc', 0))}")
    _last_regime = regime

    # --- ЗАПИСЬ ИСТОРИИ ФОНА РЫНКА (ДЛЯ ОТЧЕТОВ) ---
    paper.market_history.append({
        "ts": int(time.time()),
        "regime": regime,
        "mode": mode
    })
    # Оставляем историю только за последние 32 дня (чтобы файл не раздувался)
    cutoff = int(time.time()) - 32 * 86400
    paper.market_history = [x for x in paper.market_history if x.get("ts", 0) >= cutoff]
    paper.save()

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
                    f"🚨 <b>Экстренный выход</b> · {pair_html(ex['symbol'][:-4], ex.get('sector', 'Other'), kind_tag_of(ex), ex.get('tier'))} · "
                    f"{pnl_emoji(ex['pnl_pct'])} {ex['pnl']:+.2f}% · {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}"
                    f"{funding_line(ex.get('transferred', 0))}", urgent=True
                )
            await notify("🚨 <b>Риск-менеджмент</b>: резкий дамп рынка — всё в $.", urgent=True)
        return

# 4. УПРАВЛЕНИЕ ПОЗИЦИЯМИ
    current_time = int(time.time())
    for sym, pos in list(paper.positions.items()):
        t = tickers.get(sym)
        if not t:
            continue
        score_pos, candles = await live_score(sym, t, regime, news_items, deriv_t=deriv_tickers.get(sym))
        if score_pos is None:
            continue
        closes = [c["close"] for c in candles]
        a = atr(candles)
        if a <= 0:
            continue
        last = t["last"]
        pos["max_price"] = max(pos.get("max_price", 0.0), last)
        pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
        e21, e50 = ema(closes, 21)[-1], ema(closes, 50)[-1]
        thr = threshold(regime)
        trend_broken = last < e50 and e21 < e50

        # 4.0 НОВОСТНАЯ ПРОВЕРКА ПОЗИЦИИ
        base = sym[:-4]
        name = await get_coin_name(base)
        neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])
        
        is_toxic = neg > 0 and neg > pos_news and neg >= (mentions * 0.2)
        if is_toxic:
            _fomo_cooldowns[sym] = current_time + 7200  # пауза 2 часа
            if pnl_pct >= MIN_EARLY_EXIT_PCT:
                ex = paper._sell(sym, last, "НОВОСТИ ⚠️", regime_now=regime)
                await notify(
                    f"💸 <b>Продажа</b> · {pair_html(sym[:-4], ex.get('sector', 'Other'), kind_tag_of(ex), ex.get('tier'))} · новостной выход ⚠️ {neg}/{mentions}\n"
                    f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}{corr_txt(ex)}"
                    f"{funding_line(ex.get('transferred', 0))}"
                )
                continue
            elif pnl_pct <= 0:
                ex = paper._sell(sym, last, "НОВОСТИ 🛑", regime_now=regime)
                await notify(
                    f"💸 <b>Продажа</b> · {pair_html(sym[:-4], ex.get('sector', 'Other'), kind_tag_of(ex), ex.get('tier'))} · новостная резка 🛑 {neg}/{mentions}\n"
                    f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}{corr_txt(ex)}"
                    f"{funding_line(ex.get('transferred', 0))}"
                )
                continue

        # 4а. ЧАСТИЧНЫЙ TP
        if not pos.get("tp1_done") and last >= pos["tp"]:
            half = pos["qty"] / 2
            ex = paper.sell_partial(sym, half, pos["tp"], "TP1 🎯")
            pos["tp1_done"] = True
            
            # Честный безубыток с учетом комиссий за вход и выход (0.2%)
            breakeven_price = pos["avg"] * (1 + (FEE_PCT * 2) / 100)
            pos["sl"] = max(pos["sl"], breakeven_price)
            
            pos["tp"] = round(pos["tp"] + 1.5 * a, 10)
            paper.save()
            
            # Динамическое форматирование строки SL (как в /status)
            sl_pct = (pos["sl"] - pos["avg"]) / pos["avg"] * 100 if pos["avg"] else 0
            if sl_pct >= 0.5:
                sl_str = f"📈 <b>{fmt_price(pos['sl'])} (+{sl_pct:.2f}%)</b>"
            elif sl_pct >= 0.15: # Учитываем комиссию ~0.2%
                sl_str = f"🔒 <b>{fmt_price(pos['sl'])} (безубыток)</b>"
            else:
                sl_str = f"🛡 <b>{fmt_price(pos['sl'])} ({sl_pct:+.2f}%)</b>"
                
            await notify(
                f"🎯 <b>TP1</b> · {pair_html(sym[:-4], ex.get('sector', 'Other'), kind_tag_of(ex), ex.get('tier'))} · 50%\n"
                f"📊 {fmt_price(ex['price'])} · 🔥 {fmt_pct(ex['pnl_pct'])} · 💵 прибыль {usd(ex['pnl'])}"
                f"{funding_line(ex.get('transferred', 0))}\n"
                f"остаток бежит · 🎯 {fmt_price(pos['tp'])} · {sl_str}"
            )
            continue

        # 4б. ИНВАЛИДАЦИЯ + серая зона + regime-инвалидация
        signal_weak = trend_broken or score_pos <= thr - 2

        pos_corr = pos.get("corr", 0.5)
        btc_dependent = pos_corr >= 0.45

        regime_danger = False
        if pos.get("regime_entry") == "bull" and btc_dependent:
            if regime == "bear":
                regime_danger = True
            elif regime == "neutral" and score_pos < thr:
                regime_danger = True

        if signal_weak or (regime_danger and pnl_pct <= -0.5):
            _fomo_cooldowns[sym] = current_time + 7200  # пауза 2 часа на слом тренда
            if pnl_pct >= MIN_EARLY_EXIT_PCT:
                ex = paper._sell(sym, last, "СИГНАЛ ИСЯК 📉", regime_now=regime)
                await notify(
                    f"💸 <b>Продажа</b> · {pair_html(sym[:-4], ex.get('sector', 'Other'), kind_tag_of(ex), ex.get('tier'))} · сигнал ослаб 📉\n"
                    f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}{corr_txt(ex)}"
                    f"{funding_line(ex.get('transferred', 0))}"
                )
                continue
            if pnl_pct <= -0.5:
                ex = paper._sell(sym, last, "ИНВАЛИДАЦИЯ 🛑", regime_now=regime)
                await notify(
                    f"💸 <b>Продажа</b> · {pair_html(sym[:-4], ex.get('sector', 'Other'), kind_tag_of(ex), ex.get('tier'))} · резка убытка 🛑\n"
                    f"{pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}{corr_txt(ex)}"
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
                    f"🎯 <b>TP поднят</b> (раннер) · {pair_html(sym[:-4], pos.get('sector') or 'Other', kind_tag_of(pos), pos.get('tier'))}\n"
                    f"🎯 {fmt_price(pos['tp'])} · 🛡 {fmt_price(pos['sl'])}"
                )

        # 4г. Трейлинг SL — динамический поджим
        if new_sl is None:
            new_sl = pos["sl"]
            
        max_p = pos.get("max_price", last)
        breakeven_price = pos["avg"] * (1 + (FEE_PCT * 2) / 100)

        if max_p >= pos["avg"] + 1.0 * a:
            new_sl = max(new_sl, breakeven_price)
        if max_p >= pos["avg"] + 1.5 * a:
            new_sl = max(new_sl, max_p - 1.0 * a)
        if max_p >= pos["avg"] + 2.5 * a:
            new_sl = max(new_sl, max_p - 0.5 * a)

        if new_sl and new_sl > pos["sl"]:
            new_sl_r = round(new_sl, 10)
            if new_sl_r > pos["sl"]:
                pos["sl"] = new_sl_r
                pos["max_sl"] = max(pos.get("max_sl", 0), pos["sl"])
                logger.info(f"SL поднят {sym} -> {pos['sl']}")
        paper.save()

    # 5. Выходы остатка по TP/SL
    for ex in paper.check_exits(tickers, regime_now=regime):
        runner_txt = ""
        if ex.get("runner_bonus", 0) > 5:
            runner_txt = f"\n🏃 пробежка +{ex['runner_bonus']:.1f}% выше TP1"
        ind = "🔥" if ex.get("exit_type") == "TP1_RUN" else pnl_emoji(ex["pnl_pct"])
        await notify(
            f"💸 <b>Продажа</b> · {pair_html(ex['symbol'][:-4], ex.get('sector', 'Other'), kind_tag_of(ex), ex.get('tier'))} · {ex['reason']}\n"
            f"{ind} {fmt_pct(ex['pnl_pct'])} · 💵 {usd(ex['pnl'])} · 📊 {fmt_price(ex['price'])}{corr_txt(ex)}{runner_txt}"
            f"{funding_line(ex.get('transferred', 0))}"
        )

    # 6. Неисполненные ордера (живая проверка сигнала + реквота по offset)
    thr = threshold(regime)
    for order in list(paper.orders):
        t = tickers.get(order["symbol"])
        if not t:
            continue

        base = order["symbol"][:-4]
        o_pair = pair_html(base, order.get("sector") or "Other", kind_tag_of(order), order.get("tier"))
        name = await get_coin_name(base)
        neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])
        
        is_toxic = neg > 0 and neg > pos_news and neg >= (mentions * 0.2)
        if is_toxic:
            paper.cancel_order(order["id"])
            _fomo_cooldowns[order["symbol"]] = current_time + 7200
            await notify(f"⚠️ <b>Ордер снят</b> · {o_pair} · негатив {neg}/{mentions} (пауза 2ч)")
            continue

        score_now, candles = await live_score(order["symbol"], t, regime, news_items)
        if score_now is None:
            continue
        a = atr(candles)
        if a <= 0:
            continue
        if score_now < thr - 2:
            paper.cancel_order(order["id"])
            _fomo_cooldowns[order["symbol"]] = current_time + 7200
            await notify(f"📉 <b>Ордер снят</b> · {o_pair} · сигнал умер (пауза 2ч)")
            continue

        if (current_time - order["created"]) < 900:
            continue
        if order.get("requotes", 0) >= 2:
            paper.cancel_order(order["id"])
            _fomo_cooldowns[order["symbol"]] = current_time + 1800
            await notify(f"❌ <b>Ордер снят</b> · {o_pair} · 3 попытки без исполнения (пауза 30м)")
            continue
        if score_now < thr:
            paper.cancel_order(order["id"])
            _fomo_cooldowns[order["symbol"]] = current_time + 3600
            await notify(f"📉 <b>Ордер снят</b> · {o_pair} · сигнал ослаб (пауза 1ч)")
            continue
            
        paper.cancel_order(order["id"])
        
        atr_pct = a / t["last"] * 100
        is_mom = order.get("is_momentum", False)
        off = entry_offset(score_now, thr, regime, atr_pct, is_mom)
        
        ideal_price = t["last"] * (1 + off)
        old_price = order["price"]
        
        if is_mom:
            # Для ракеты разрешаем погоню (до 3 раз), стоп короткий
            order["price"] = ideal_price
            price_icon = "⬆️" if ideal_price > old_price else "⬇️"
            sl_dist = 0.6 * a  # Короткий стоп (-0.6 ATR)
        else:
            # Для снайпера: если улетела больше чем на 1.5 ATR - снимаем
            if t["last"] > old_price + 1.5 * a:
                _fomo_cooldowns[order["symbol"]] = current_time + 1800
                await notify(f"🚀 <b>Ордер снят (Улетела)</b> · {o_pair} · пауза 30м")
                continue
            # Лимитку вверх не двигаем, только вниз или на месте
            if ideal_price > old_price:
                order["price"] = old_price
                price_icon = "⏸"
            else:
                order["price"] = ideal_price
                price_icon = "⬇️"
            sl_dist = 1.2 * a  # Стандартный стоп

        order["tp"] = max(order["price"] + 2.0 * a, order["price"] * (1 + MIN_TP_PCT / 100))
        order["sl"] = min(order["price"] - sl_dist, order["price"] * (1 - MIN_SL_PCT / 100))
        order["created"] = current_time
        order["requotes"] = order.get("requotes", 0) + 1
        paper.orders.append(order)
        paper.save()
        
        mode_txt = "Ракета" if is_mom else "Снайпер"
        await notify(
            f"🔁 <b>Ордер {price_icon}</b> · {o_pair} ({mode_txt})\n"
            f"📥 {fmt_price(order['price'])} ({off * 100:+.2f}%) · попытка {order['requotes'] + 1}"
        )

# 7. Сканирование и покупки / ротация
    candidates = await scan(regime, tickers, deriv_tickers, limit=20)

    equity = paper.equity(tickers)
    thr = threshold(regime)
    sec_lim, other_lim = portfolio_limits(equity)
    sat_limit = learner.satellite_limit()
    sat_size = learner.satellite_size_pct()
    logger.info(f"PORTFOLIO LIMITS: equity={equity:.0f} | "
                f"лимит на сектор={sec_lim} | лимит Other={other_lim} | "
                f"лимит сателлитов={sat_limit:.0f}% · размер сателлита={sat_size:.1f}%")

    # Очистка старых кулдаунов (чтобы не копились в памяти бесконечно)
    current_time = int(time.time())
    for k in list(_fomo_cooldowns.keys()):
        if _fomo_cooldowns[k] < current_time:
            del _fomo_cooldowns[k]

    for cand in candidates:
        sym = cand["symbol"]
        
        # Если монета на паузе (кулдаун еще не истек) — пропускаем её
        if _fomo_cooldowns.get(sym, 0) > current_time:
            continue
            
        if sym in paper.positions or any(o["symbol"] == sym for o in paper.orders):
            continue

        kind = cand.get("kind", "core")
        sector = cand.get("sector", "Other")

        # --- Проверка лимитов сектора и внутрисекторная ротация ---
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
            if sector_positions:
                weakest_sym, weakest_pos = min(sector_positions, key=lambda kv: kv[1].get("score", 0))
                weakest_score = weakest_pos.get("score", 0)
                t_weak = tickers.get(weakest_sym)
                if t_weak:
                    weak_pnl = (t_weak["last"] - weakest_pos["avg"]) / weakest_pos["avg"] * 100
                    
                    can_rotate = False
                    if sector == "Other":
                        can_rotate = cand["score"] >= weakest_score + 1.0 and not weakest_pos.get("tp1_done") and weak_pnl >= -2.0
                    else:
                        can_rotate = cand["score"] >= weakest_score + 1.5 and not weakest_pos.get("tp1_done") and -0.5 <= weak_pnl < 2.0

                    if can_rotate:
                        ex = paper._sell(weakest_sym, t_weak["last"], "РОТАЦИЯ СЕКТОРА 🔄", regime_now=regime)
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
            
            if not rotated:
                logger.info(f"{sym}: пропущен — сектор {sector} переполнен, слабаков для ротации нет")
                continue

        # --- Проверка лимита сателлитов ---
        if kind == "satellite":
            sat_exposure = sum(
                p["qty"] * tickers.get(s, {}).get("last", 0)
                for s, p in paper.positions.items() if p.get("kind"] == "satellite"
            ) + sum(
                o["qty"] * o["price"] for o in paper.orders if o.get("kind"] == "satellite"
            )
            if sat_exposure >= equity * sat_limit / 100:
                logger.info(f"{sym}: пропущен — лимит сателлитов исчерпан ({sat_limit:.0f}%)")
                continue

        # --- Расчет точек входа и стопов ---
        is_mom = cand.get("is_momentum", False)
        off = entry_offset(cand["score"], thr, regime, cand["atr_pct"], is_mom)
        
        t_data = tickers.get(sym, {})
        bid1 = t_data.get("bid1", cand["last"])
        ideal_entry = cand["last"] * (1 + off)
        
        if is_mom:
            entry = ideal_entry  # Ракета бьет по рынку
        else:
            entry = min(ideal_entry, bid1)  # Снайпер встает лимиткой в стакан
            
        a = cand["atr"]
        if a <= 0:
            logger.info(f"{sym}: пропущен — нулевой ATR")
            continue

        if kind == "satellite":
            base_sl_mult = 0.75 if is_mom else 1.5
            sl_dist_pct = max(min(base_sl_mult * a / entry * 100 * shadow.sl_mult(), SAT_MAX_SL_PCT), 2.0)
            tp_dist_pct = max(min(2.5 * a / entry * 100 * shadow.tp_mult(), 12.0), sl_dist_pct * MIN_RR_SAT)
            sl = entry * (1 - sl_dist_pct / 100)
            tp = entry * (1 + tp_dist_pct / 100)
            min_rr = MIN_RR_SAT
        else:
            sl_dist_atr = 0.6 * a if is_mom else 1.2 * a
            sl_dist_raw = sl_dist_atr * shadow.sl_mult()
            tp_dist_raw = max(2.0 * a * shadow.tp_mult(), sl_dist_raw * MIN_RR)
            sl = entry - sl_dist_raw
            tp = entry + tp_dist_raw
            tp = max(tp, entry * (1 + MIN_TP_PCT / 100))
            sl = min(sl, entry * (1 - MIN_SL_PCT / 100))
            sl_dist_pct = (entry - sl) / entry * 100
            min_rr = MIN_RR
            if sl_dist_pct > MAX_SL_PCT:
                logger.info(f"{sym}: пропущен — SL слишком далеко ({sl_dist_pct:.1f}%)")
                continue

        sl_dist = (entry - sl) / entry * 100
        rr = (tp - entry) / (entry - sl) if entry > sl else 0
        if tp <= entry or sl >= entry or round(rr, 2) < min_rr:
            logger.info(f"{sym}: пропущен — плохой Risk/Reward (R:R = {rr:.2f})")
            continue

        entry_mode = "rocket" if is_mom else "sniper"
        km = learner.kelly_multiplier(entry_mode)  # Индивидуальный келли
        
        size = buy_size(equity, cand["score"], cand["liquidity"], paper.usdt,
                        sl_dist, kind=kind, km=km,
                        sat_size_pct=sat_size, size_multiplier=cand.get("size_mult", 1.0))
        if size < 5:
            logger.info(f"{sym}: пропущен — размер позиции < 5$")
            continue

        # --- Проверка баланса и ОБЩАЯ РОТАЦИЯ ---
        pending_amount = sum(o["qty"] * o["price"] for o in paper.orders)
        if pending_amount + size > paper.usdt:
            freed_space = False
            
            # 1. Пытаемся снять слабый ордер
            if paper.orders:
                worst_order = min(paper.orders, key=lambda o: o.get("score", 0))
                worst_score = worst_order.get("score", 0)
                if cand["score"] >= worst_score + 1.0:
                    paper.cancel_order(worst_order["id"])
                    pending_amount -= worst_order["qty"] * worst_order["price"]
                    await notify(
                        f"🔄 <b>Ротация ордера</b> · <b>{worst_order['symbol'][:-4]}</b> снят\n"
                        f"Место для <b>{sym[:-4]}</b> · ⭐ {worst_score:.1f} → {cand['score']:.1f}"
                    )
                    logger.info(f"{sym}: ротация — снят ордер {worst_order['symbol']}")
                    freed_space = True

            # 2. Если всё еще нет денег, пытаемся продать слабую позицию
            if (pending_amount + size > paper.usdt) and paper.positions:
                worst_sym, worst_pos = min(paper.positions.items(), key=lambda kv: kv[1].get("score", 0))
                worst_score = worst_pos.get("score", 0)
                t_weak = tickers.get(worst_sym)
                if t_weak:
                    weak_pnl = (t_weak["last"] - worst_pos["avg"]) / worst_pos["avg"] * 100
                    if cand["score"] >= worst_score + 1.5 and not worst_pos.get("tp1_done") and weak_pnl >= -2.0:
                        ex = paper._sell(worst_sym, t_weak["last"], "ОБЩАЯ РОТАЦИЯ 🔄", regime_now=regime)
                        await notify(
                            f"🔄 <b>Общая ротация</b> · <b>{worst_sym[:-4]}</b> → <b>{sym[:-4]}</b>\n"
                            f"{pnl_emoji(weak_pnl)} {fmt_pct(weak_pnl)} · ⭐ {worst_score:.1f} → {cand['score']:.1f}\n"
                            f"💵 {usd(ex['pnl'])}{funding_line(ex.get('transferred', 0))}"
                        )
                        logger.info(f"{sym}: общая ротация — продан {worst_sym}")
                        freed_space = True

            # Если места так и не появилось — пропускаем этого кандидата и идем к следующему
            if pending_amount + size > paper.usdt:
                logger.info(f"{sym}: пропущен — нет свободных средств и слабаков для ротации")
                continue

        # --- Финальное выставление ордера ---
        qty = size / entry
        order = paper.place_limit_buy(sym, qty, entry, tp=tp, sl=sl,
                                      score=cand["score"],
                                      reason_keys=cand.get("reason_keys", []))
        order["kind"] = kind
        order["sector"] = sector
        order["tier"] = cand.get("tier")
        order["corr"] = cand.get("corr")
        order["regime"] = regime
        order["is_momentum"] = is_mom
        paper.save()
        tp_pct = (tp - entry) / entry * 100
        sl_pct = (sl - entry) / entry * 100
        kind_tag = "🛰" if kind == "satellite" else "🏛"
        new_tag = "· 🆕 " if cand.get("is_new") else ""
        mode_tag = "🚀 Ракета" if is_mom else "🏹 Снайпер"
        await notify(
            f"📋 <b>Ордер ({mode_tag})</b> {new_tag}· {pair_html(sym[:-4], sector, kind_tag, cand.get('tier'))}\n"
            f"💵 {usd(size)} · 📥 {fmt_price(entry)} ({off * 100:+.2f}%){corr_txt(cand)}\n"
            f"🎯 {fmt_price(tp)} ({fmt_pct(tp_pct)}) · 🛡 {fmt_price(sl)} ({fmt_pct(sl_pct)})\n"
            f"⭐ {cand['score']:.1f} · 🧠 {'; '.join(cand['reasons'][:3])}"
        )

    # --- СБРОС И ОТПРАВКА БУФЕРА УВЕДОМЛЕНИЙ ---
    if _notification_buffer:
        digest_text = "⚡️ <b>Цикл торговли · Дайджест</b>\n\n" + "\n\n".join(_notification_buffer)
        _notification_buffer.clear()
        if _notify_cb:
            try:
                await _notify_cb(digest_text)
            except Exception as e:
                logger.error(f"digest notify error: {e}")

    logger.info("=== CYCLE END ===")
