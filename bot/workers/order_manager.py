import time
import asyncio
from collections import defaultdict
from loguru import logger

from bot.core.event_bus import EventBus
from bot.exchange.paper_exchange import paper
from bot.strategy.sizing import (
    buy_size, entry_offset, min_order_threshold,
    MAX_POSITIONS_PER_TIER, MAX_ORDERS_PER_TIER
)
from bot.strategy.scanner import get_thresholds
from bot.strategy.learner import learner
from bot.strategy.risk import (
    calc_initial_sl_tp, get_trade_policy,
    evaluate_setup_grade, calc_total_open_risk_pct
)
from bot.news.cmc import get_coin_name
from bot.news.rss_news import fetch_news_cache, check_sentiment
from bot.utils.format import pair_html, corr_txt, usd, fmt_price, fmt_pct, pnl_emoji, grade_badge
from bot.core.state import bot_state


class OrderManagerWorker:
    """Управляет портфелем: Умный Овербукинг, безопасная ротация спящих слотов и защита от покупки шпилек."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.signal_queue = self.bus.subscribe("SIGNALS_READY", maxsize=2)

    async def run(self):
        logger.info("💼 Order Manager запущен: Умный Овербукинг, выверенные точки входа и защита от суеты...")
        while True:
            try:
                event = await self.signal_queue.get()
                await self._process_signals(event.payload)
                self.signal_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"OrderManager error: {e}")

    def _notify(self, text: str, urgent: bool = False):
        self.bus.publish("NOTIFY", {"text": text, "urgent": urgent})

    def _effective_orders_for_risk(self) -> list:
        if not paper.orders:
            return []
        by_tier = defaultdict(list)
        for o in paper.orders:
            by_tier[o.get("tier") or "SMALL"].append(o)

        effective = []
        for t, ords in by_tier.items():
            avail_slots = max(0, MAX_POSITIONS_PER_TIER - paper.count_tier_positions(t))
            if avail_slots > 0:
                ords_sorted = sorted(
                    ords,
                    key=lambda x: (1 if x.get("grade") == "A+" else 0, float(x.get("score", 0.0))),
                    reverse=True,
                )
                effective.extend(ords_sorted[:avail_slots])
        return effective

    def _try_evict_sleeping_position(self, cand: dict, tier: str, tickers: dict, regime: str) -> bool:
        """
        Вызывается ТОЛЬКО тогда, когда новая ракета (score >= 9.2) УЖЕ прошла все проверки стен и RR!
        Не трогает свежие донные позиции (reversal / 1H_DIP_BOUNCE), чтобы не выбрасывать сделки на дне (фикс INJUSDT).
        """
        if cand.get("score", 0.0) < 9.2 or cand.get("entry_mode") not in ("sniper", "rocket"):
            return False

        now_ts = int(time.time())
        sleeping_candidates = []

        for sym, pos in list(paper.positions.items()):
            if (pos.get("tier") or "SMALL") != tier:
                continue
            if pos.get("tp1_done"):
                continue
            pos_age = now_ts - int(pos.get("entry_time", now_ts))
            min_sleep_age = 9000 if (pos.get("entry_mode") == "reversal" or pos.get("gate_1h") == "1H_DIP_BOUNCE") else 6300
            if pos_age < min_sleep_age:
                continue
            last_p = tickers.get(sym, {}).get("last", 0.0)
            avg_p = pos.get("avg", 0.0)
            if last_p <= 0 or avg_p <= 0:
                continue
            pnl_pct = (last_p - avg_p) / avg_p * 100.0
            if -0.05 <= pnl_pct <= 0.35 and (cand["score"] - float(pos.get("score", 6.0))) >= 1.6:
                sleeping_candidates.append((pos_age, sym, pos, last_p))

        if not sleeping_candidates:
            return False

        sleeping_candidates.sort(key=lambda x: x[0], reverse=True)
        _, v_sym, v_pos, v_last = sleeping_candidates[0]
        reason = f"🔄 Ротация слота под ракету {cand['symbol'][:-4]} ({cand['score']:.1f})"
        ex = paper._sell(v_sym, v_last, "🛡🔒 БУ", regime_now=regime)
        paper.log_event(v_sym, "sell_profit", v_last, reason, tp=v_pos.get("tp"), sl=v_pos.get("sl"), sup=v_pos.get("nearest_sup"), res=v_pos.get("nearest_res"))
        bot_state.set_cooldown(v_sym, 600, structural_reset=False, reason="slot_evict")
        logger.info(f"OrderManager: 🔄 Освобождён спящий слот {v_sym} ({ex['pnl_pct']:+.2f}%) ради валидной ракеты {cand['symbol']} ({cand['score']:.1f})")
        self._notify(
            f"🔄 <b>Освобождён спящий слот</b> · продан {pair_html(v_sym, ex)}{grade_badge(ex)} ({pnl_emoji(ex['pnl_pct'])} {fmt_pct(ex['pnl_pct'])})\n"
            f"➡️ под ракету <b>{cand['symbol'][:-4]}</b> (⭐ {cand['score']:.1f})"
        )
        return True

    def _try_rotate_order(self, cand: dict, cand_grade: str, tickers: dict, reason_tag: str = "освобождение USDT", target_tier: str = None) -> bool:
        if not paper.orders:
            return False

        now_ts = int(time.time())
        cand_score = cand.get("score", 0.0)
        eligible = []

        for o in paper.orders:
            if target_tier and (o.get("tier") or "SMALL") != target_tier:
                continue

            o_age = now_ts - o.get("created", now_ts)
            o_grade = o.get("grade", "B")
            o_score = o.get("score", 0.0)

            # Запрещаем дёргать ордера младше 15 минут (900 сек) — убираем пустую суету без сделок (фикс TAUSDT, ARXUSDT)!
            if o_age < 900:
                continue

            last_p = tickers.get(o["symbol"], {}).get("last", 0.0)
            if last_p > 0 and abs(last_p - o["price"]) / o["price"] * 100 <= 0.45:
                continue

            score_diff = cand_score - o_score
            is_upgrade = (
                (score_diff >= 1.4) or
                (cand_grade == "A+" and o_grade != "A+" and score_diff >= 0.8)
            )
            if is_upgrade:
                priority_key = (1 if o_grade == "A+" else 0, o_score, -o_age)
                eligible.append((priority_key, o))

        if not eligible:
            return False

        eligible.sort(key=lambda x: x[0])
        victim = eligible[0][1]
        v_sym = victim["symbol"]
        v_last = tickers.get(v_sym, {}).get("last", victim["price"])

        paper.cancel_order(victim["id"])
        paper.log_event(
            v_sym, "cancel", v_last,
            f"Ротация ({reason_tag}) -> {cand['symbol'][:-4]} ({victim.get('score', 0):.1f} -> {cand_score:.1f})",
            tp=victim.get("tp"), sl=victim.get("sl"),
            sup=victim.get("nearest_sup"), res=victim.get("nearest_res")
        )
        logger.info(
            f"OrderManager: 🔄 РОТАЦИЯ ОРДЕРА ({reason_tag}): снят {v_sym} "
            f"(score {victim.get('score', 0):.1f}{grade_badge(victim)}) ради {cand['symbol']} "
            f"(score {cand_score:.1f}{' [💎]' if cand_grade == 'A+' else ''})"
        )
        self._notify(
            f"🔄 <b>Ротация ({reason_tag})</b> · снят {pair_html(v_sym, victim)}{grade_badge(victim)} ({victim.get('score', 0):.1f}) "
            f"➡️ под <b>{cand['symbol'][:-4]}</b>{' [💎]' if cand_grade == 'A+' else ''} ({cand_score:.1f})"
        )
        return True

    async def _process_signals(self, payload: dict):
        candidates = payload["candidates"]
        tickers = payload["tickers"]
        regime = payload["regime"]

        equity = paper.equity(tickers)
        thrs = get_thresholds(regime)
        news_items = await fetch_news_cache()

        risk_mode = getattr(learner, "current_risk_mode", "NORMAL")
        policy = get_trade_policy(risk_mode)
        min_usdt_order = min_order_threshold(equity)

        eff_risk_now = calc_total_open_risk_pct(paper.positions, self._effective_orders_for_risk(), equity)
        logger.info(
            f"💼 OrderManager [{risk_mode}]: кандидатов {len(candidates)}, "
            f"эффективный открытый риск {eff_risk_now:.2f}% / {policy['max_portfolio_risk_pct']}%"
        )

        for cand in candidates:
            sym = cand["symbol"]
            base = sym[:-4]

            if sym in paper.positions or any(o["symbol"] == sym for o in paper.orders):
                continue

            sector = cand.get("sector", "Other")
            tier = cand.get("tier") or "SMALL"
            entry_mode = cand.get("entry_mode", "sniper")
            is_mom = cand.get("is_momentum", False)

            tier_pos_count = paper.count_tier_positions(tier)
            needs_eviction = False
            if tier_pos_count >= MAX_POSITIONS_PER_TIER:
                if cand.get("score", 0.0) >= 9.2 and entry_mode in ("sniper", "rocket"):
                    needs_eviction = True
                else:
                    logger.debug(f"OrderManager: {sym} пропущен -> в тире {tier} уже открыто {tier_pos_count}/{MAX_POSITIONS_PER_TIER} позиций")
                    continue

            cand_corr = float(cand.get("corr") or 0.0)
            if regime in ("bear", "neutral") and cand_corr >= 0.85 and cand.get("score", 0.0) < 9.0:
                max_corr_pos = 3 if regime == "bear" else 4
                high_corr_pos = sum(1 for p in paper.positions.values() if float(p.get("corr") or 0.0) >= 0.85)
                high_corr_ord = sum(1 for o in paper.orders if float(o.get("corr") or 0.0) >= 0.85)
                if high_corr_pos >= max_corr_pos or (high_corr_pos + high_corr_ord) >= (max_corr_pos + 1):
                    logger.info(f"OrderManager: {sym} пропущен -> лимит BTC-клонов ({high_corr_pos} поз., {high_corr_ord} орд., corr {cand_corr:.2f})")
                    continue

            patterns = cand.get("patterns", [])
            is_accum = "vcp" in patterns
            e21_15m = cand.get("e21", 0.0)
            e50_15m = cand.get("e50", 0.0)
            a = cand["atr"]
            if a <= 0:
                continue

            if learner.coin_bias(sym) > -1.0:
                bot_state.check_structural_reset(sym, cand["last"], e21_15m, e50_15m)
            if bot_state.is_on_cooldown(sym):
                continue

            if entry_mode == "reversal":
                can_rev, rev_reason = bot_state.can_enter_reversal(sym, cand["last"], e21_15m)
                if not can_rev:
                    logger.info(f"OrderManager: {sym} reversal заблокирован -> {rev_reason}")
                    continue
            elif entry_mode == "sniper":
                can_snp, snp_reason = bot_state.can_enter_sniper(
                    sym, cand["last"], patterns,
                    ema21_15m=e21_15m, ema50_15m=e50_15m, atr_15m=a,
                    score=cand.get("score", 0.0), gate_1h=cand.get("gate_1h", "")
                )
                if not can_snp:
                    logger.info(f"OrderManager: {sym} sniper заблокирован -> {snp_reason}")
                    continue

            grade = evaluate_setup_grade(cand)
            if policy["require_grade_a_for_alt"] and entry_mode in ("reversal", "rocket"):
                if grade != "A+":
                    logger.info(f"OrderManager: {sym} пропущен -> режим {risk_mode} требует [💎] A+ для {entry_mode}")
                    continue

            avail_tier_slots = max(1 if needs_eviction else 0, MAX_POSITIONS_PER_TIER - tier_pos_count)
            is_elite_cand = (grade == "A+" or cand["score"] >= 8.2)
            if avail_tier_slots == 1:
                tier_orders = [o for o in paper.orders if (o.get("tier") or "SMALL") == tier]
                has_elite_waiting = any(o.get("grade") == "A+" or float(o.get("score", 0.0)) >= 8.0 for o in tier_orders)
                if has_elite_waiting and not is_elite_cand:
                    logger.debug(f"OrderManager: {sym} [B] пропущен -> последний слот тира {tier} забронирован под [💎] A+")
                    continue

            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])
            if neg >= 2 and neg >= (pos_news * 2) and neg >= (mentions * 0.33):
                logger.info(f"OrderManager: {sym} отклонен -> токсичные новости ({neg}/{mentions})")
                bot_state.set_cooldown(sym, 7200)
                continue

            thr = cand.get("thr") or thrs.get(entry_mode, 6.0)
            off = entry_offset(cand["score"], thr, regime, cand["atr_pct"], entry_mode, tier=tier, is_accumulation=is_accum, symbol=sym)

            t_data = tickers.get(sym, {})
            bid1 = t_data.get("bid1", cand["last"])
            ideal_entry = cand["last"] * (1 + off)
            entry = min(ideal_entry, bid1 * 0.9990)

            # ЗАЩИТА ОТ ВХОДА НА МИНУТНЫХ ШПИЛЬКАХ (фикс TAUSDT):
            # Для любого sniper (кроме старта со дна 1H_DIP_BOUNCE) лимитка ставится не выше 15m EMA21 + 0.30*ATR!
            if entry_mode == "sniper" and cand.get("gate_1h") != "1H_DIP_BOUNCE" and e21_15m > 0:
                ema_pullback_cap = e21_15m + 0.30 * a
                if ema_pullback_cap < entry and ema_pullback_cap >= cand["last"] * 0.986:
                    entry = ema_pullback_cap

            wick_entry = cand.get("wick_entry_price")
            if entry_mode == "reversal" and wick_entry and 0 < wick_entry < entry:
                entry = max(wick_entry, cand["last"] * 0.990)

            sup = cand.get("nearest_sup")
            sup_touches = cand.get("sup_touches", 0)
            if sup and sup_touches >= 2 and not is_mom:
                retest_floor = sup * 1.0012
                if entry < sup and retest_floor <= cand["last"] * 0.9980:
                    entry = retest_floor

            off = (entry - cand["last"]) / cand["last"] if cand["last"] > 0 else off

            all_res = cand.get("signal_values", {}).get("all_res", [])
            res_above_entry = []
            for r_item in all_res:
                r_val, r_tch = r_item if isinstance(r_item, (list, tuple)) else (r_item, 2)
                if r_val > entry * 1.0005:
                    res_above_entry.append((r_val, r_tch))

            if res_above_entry:
                res_above_entry.sort(key=lambda x: x[0])
                entry_res, res_touches = res_above_entry[0]
            else:
                entry_res = cand.get("nearest_res")
                res_touches = cand.get("res_touches", 0)

            min_rr = policy["min_rr_sniper"] if entry_mode == "sniper" else policy["min_rr_alt"]

            sl, tp, is_valid, rej_reason = calc_initial_sl_tp(
                symbol=sym, entry=entry, tier=tier, a=a,
                sup=sup, sup_touches=sup_touches,
                res=entry_res, res_touches=res_touches,
                is_mom=is_mom, entry_mode=entry_mode,
                min_rr=min_rr,
                recent_low_6=cand.get("recent_low_6"),
                ema50=e50_15m,
                e21_1h=cand.get("e21_1h"),
                e50_1h=cand.get("e50_1h"),
            )

            if not is_valid:
                logger.info(f"OrderManager: {sym} отклонён -> {rej_reason}")
                continue

            sl_dist_pct = (entry - sl) / entry * 100
            if sl_dist_pct <= 0:
                continue

            # Только ТЕПЕРЬ, когда кандидат 100% валиден и прошёл проверку стен и RR, освобождаем под него спящий слот!
            if needs_eviction:
                if self._try_evict_sleeping_position(cand, tier, tickers, regime):
                    tier_pos_count = paper.count_tier_positions(tier)
                    avail_tier_slots = max(0, MAX_POSITIONS_PER_TIER - tier_pos_count)
                else:
                    continue

            max_tier_orders = 2 if avail_tier_slots == 1 else min(MAX_ORDERS_PER_TIER, avail_tier_slots + 2)
            tier_ord_count = paper.count_tier_orders(tier)
            if tier_ord_count >= max_tier_orders:
                if not self._try_rotate_order(cand, grade, tickers, reason_tag=f"буфер тира {tier}", target_tier=tier):
                    logger.debug(f"OrderManager: {sym} пропущен -> в тире {tier} уже {tier_ord_count}/{max_tier_orders} ордеров")
                    continue

            trade_risk_budget_pct = policy["risk_per_trade_core"] if entry_mode == "sniper" else policy["risk_per_trade_alt"]
            current_risk_pct = calc_total_open_risk_pct(paper.positions, self._effective_orders_for_risk(), equity)
            avail_risk_budget_pct = policy["max_portfolio_risk_pct"] - current_risk_pct

            if avail_risk_budget_pct <= 0.15:
                if self._try_rotate_order(cand, grade, tickers, reason_tag="лимит Portfolio VaR"):
                    current_risk_pct = calc_total_open_risk_pct(paper.positions, self._effective_orders_for_risk(), equity)
                    avail_risk_budget_pct = policy["max_portfolio_risk_pct"] - current_risk_pct

            if avail_risk_budget_pct <= 0.15:
                logger.debug(f"OrderManager: {sym} пропущен -> лимит риска исчерпан")
                continue

            effective_risk_pct = min(trade_risk_budget_pct, avail_risk_budget_pct)
            size_by_risk = (equity * (effective_risk_pct / 100.0)) / (sl_dist_pct / 100.0)

            locked_usdt = paper.effective_locked_usdt()
            free_usdt = max(0.0, paper.usdt - locked_usdt)

            if free_usdt < min_usdt_order:
                if self._try_rotate_order(cand, grade, tickers, reason_tag="нехватка свободного USDT"):
                    locked_usdt = paper.effective_locked_usdt()
                    free_usdt = max(0.0, paper.usdt - locked_usdt)

            size_base = buy_size(
                equity, cand["score"], thr, cand["liquidity"], free_usdt,
                entry_mode=entry_mode, grade=grade, symbol=sym
            )

            size = min(size_base, size_by_risk, free_usdt * 0.98, equity * 0.14)
            size = round(size, 2)

            if size < min_usdt_order:
                logger.debug(f"OrderManager: {sym} пропущен (свободно ${free_usdt:.2f} < мин. порога ${min_usdt_order:.2f})")
                continue

            qty = size / entry
            order = paper.place_limit_buy(sym, qty, entry, tp=tp, sl=sl, score=cand["score"], reason_keys=cand.get("reason_keys", []))

            order.update({
                "sector": sector, "tier": tier, "corr": cand.get("corr"),
                "regime": regime, "risk_mode": risk_mode,
                "thr": thr,
                "gate_1h": cand.get("gate_1h", "1H_TREND_OK"),
                "reason_1h": cand.get("reason_1h", ""),
                "chg_48h": cand.get("chg_48h", 0.0),
                "pos_in_48h": cand.get("pos_in_48h", 0.5),
                "reasons": cand.get("reasons", []),
                "is_momentum": is_mom, "entry_mode": entry_mode,
                "nearest_res": entry_res,
                "res_touches": res_touches,
                "nearest_sup": sup,
                "sup_touches": sup_touches,
                "sl_state": "SHIELD",
                "patterns": patterns,
                "grade": grade,
                "wave_class": cand.get("wave_class", "mid_wave"),
                "wave_amp_pct": cand.get("wave_amp_pct", 3.2),
                "initial_score": cand["score"],
                "last_move_ts": int(order["created"]),
                "_ema21": e21_15m,
                "_ema50": e50_15m,
                "_atr15": a,
                "e21_1h": cand.get("e21_1h"),
                "e50_1h": cand.get("e50_1h"),
            })
            paper.save()

            reasons_html = "<br>".join([f"• {r}" for r in cand.get("reasons", [])])
            paper.log_event(sym, "order_placed", entry, text=reasons_html, mode=entry_mode, tp=tp, sl=sl, sup=sup, res=entry_res)

            tp_pct = (tp - entry) / entry * 100
            sl_pct = (sl - entry) / entry * 100
            g_badge = " [💎]" if grade == "A+" else ""

            logger.info(
                f"OrderManager: 🎯 {sym} ОРДЕР {entry_mode}{g_badge}: entry=${fmt_price(entry)}, "
                f"TP=${fmt_price(tp)}, SL=${fmt_price(sl)} | 1H: {cand.get('reason_1h', '')}"
            )

            self._notify(
                f"📋 <b>Ордер</b> · {pair_html(sym, order)}{g_badge}\n"
                f"💵 {usd(size)} · 📥 {fmt_price(entry)} ({off * 100:+.2f}%){corr_txt(cand)}\n"
                f"🎯 {fmt_price(tp)} ({fmt_pct(tp_pct)}) · 🛡📉 {fmt_price(sl)} ({fmt_pct(sl_pct)})\n"
                f"⭐ {cand['score']:.1f}/{thr:g} · 🕐 {cand.get('reason_1h', '')}"
            )
