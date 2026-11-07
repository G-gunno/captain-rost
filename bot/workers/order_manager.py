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
from bot.utils.format import pair_html, corr_txt, usd, fmt_price, fmt_pct, grade_badge
from bot.core.state import bot_state


class OrderManagerWorker:
    """Управляет портфелем: Умный Овербукинг (3 поз. / до 5 орд. на тир), честный резерв USDT и защита от клонов BTC."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.signal_queue = self.bus.subscribe("SIGNALS_READY", maxsize=2)

    async def run(self):
        logger.info("💼 Order Manager запущен: Умный Овербукинг, 3 поз./тир и анти-клон фильтр BTC...")
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
        """Возвращает только те ожидающие ордера в каждом тире, которые реально могут исполниться до лимита 3 позиций."""
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

    def _try_rotate_order(self, cand: dict, cand_grade: str, tickers: dict, reason_tag: str = "освобождение USDT", target_tier: str = None) -> bool:
        """Снимает старый слабый ордер при нехватке USDT/риска или при переполнении буфера ордеров конкретного тира."""
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

            min_age = 180 if target_tier else (300 if (cand_grade == "A+" and o_grade != "A+") else 600)
            if o_age < min_age:
                continue

            last_p = tickers.get(o["symbol"], {}).get("last", 0.0)
            if last_p > 0 and abs(last_p - o["price"]) / o["price"] * 100 <= 0.35:
                continue

            score_diff = cand_score - o_score
            is_upgrade = (score_diff >= 0.8) or (cand_grade == "A+" and o_grade != "A+" and score_diff >= 0.3)
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

            # 1. ПРОВЕРКА ЛИМИТА ТИРА ПО ОТКРЫТЫМ ПОЗИЦИЯМ (макс. 3 позиции одного тира)
            tier_pos_count = paper.count_tier_positions(tier)
            if tier_pos_count >= MAX_POSITIONS_PER_TIER:
                logger.debug(f"OrderManager: {sym} пропущен -> в тире {tier} уже открыто {tier_pos_count}/{MAX_POSITIONS_PER_TIER} позиций")
                continue

            # 2. Защита от синхронного набора клонов BTC (corr >= 0.75) в BEAR и NEUTRAL
            cand_corr = float(cand.get("corr") or 0.0)
            if regime in ("bear", "neutral") and cand_corr >= 0.75:
                max_corr_pos = 2 if regime == "bear" else 3
                high_corr_pos = sum(1 for p in paper.positions.values() if float(p.get("corr") or 0.0) >= 0.75)
                high_corr_ord = sum(1 for o in paper.orders if float(o.get("corr") or 0.0) >= 0.75)
                if high_corr_pos >= max_corr_pos or (high_corr_pos + high_corr_ord) >= (max_corr_pos + 1):
                    logger.info(f"OrderManager: {sym} пропущен -> лимит BTC-клонов ({high_corr_pos} поз., {high_corr_ord} орд., corr {cand_corr:.2f})")
                    continue

            patterns = cand.get("patterns", [])
            is_accum = "vcp" in patterns
            e21_15m = cand.get("e21", 0.0)
            e50_15m = cand.get("e50", 0.0)

            bot_state.check_structural_reset(sym, cand["last"], e21_15m, e50_15m)
            if bot_state.is_on_cooldown(sym):
                continue

            if entry_mode == "reversal":
                can_rev, rev_reason = bot_state.can_enter_reversal(sym, cand["last"], e21_15m)
                if not can_rev:
                    logger.info(f"OrderManager: {sym} reversal заблокирован -> {rev_reason}")
                    continue
            elif entry_mode == "sniper":
                can_snp, snp_reason = bot_state.can_enter_sniper(sym, cand["last"], patterns)
                if not can_snp:
                    logger.info(f"OrderManager: {sym} sniper заблокирован -> {snp_reason}")
                    continue

            grade = evaluate_setup_grade(cand)
            if policy["require_grade_a_for_alt"] and entry_mode in ("reversal", "rocket"):
                if grade != "A+":
                    logger.info(f"OrderManager: {sym} пропущен -> режим {risk_mode} требует [💎] A+ для {entry_mode}")
                    continue

            # 3. Умный Овербукинг ордеров внутри тира (до +2 запасных ордера сверх свободных слотов, макс. 5)
            avail_tier_slots = MAX_POSITIONS_PER_TIER - tier_pos_count
            max_tier_orders = min(MAX_ORDERS_PER_TIER, avail_tier_slots + 2)
            tier_ord_count = paper.count_tier_orders(tier)
            if tier_ord_count >= max_tier_orders:
                if not self._try_rotate_order(cand, grade, tickers, reason_tag=f"буфер тира {tier}", target_tier=tier):
                    logger.debug(f"OrderManager: {sym} пропущен -> в тире {tier} уже {tier_ord_count}/{max_tier_orders} ордеров")
                    continue

            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])
            if neg >= 2 and neg >= (pos_news * 2) and neg >= (mentions * 0.33):
                logger.info(f"OrderManager: {sym} отклонен -> токсичные новости ({neg}/{mentions})")
                bot_state.set_cooldown(sym, 7200)
                continue

            thr = cand.get("thr") or thrs.get(entry_mode, 6.0)
            off = entry_offset(cand["score"], thr, regime, cand["atr_pct"], entry_mode, tier=tier, is_accumulation=is_accum)

            t_data = tickers.get(sym, {})
            bid1 = t_data.get("bid1", cand["last"])
            ideal_entry = cand["last"] * (1 + off)
            entry = min(ideal_entry, bid1 * 0.9990)

            wick_entry = cand.get("wick_entry_price")
            if entry_mode == "reversal" and wick_entry and 0 < wick_entry < entry:
                entry = max(wick_entry, cand["last"] * 0.988)

            sup = cand.get("nearest_sup")
            sup_touches = cand.get("sup_touches", 0)
            if sup and sup_touches >= 2 and not is_mom:
                retest_floor = sup * 1.0012
                if entry < sup and retest_floor <= cand["last"] * 0.9975:
                    entry = retest_floor

            off = (entry - cand["last"]) / cand["last"] if cand["last"] > 0 else off

            a = cand["atr"]
            if a <= 0:
                continue

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

            # Считаем свободный USDT через Умный Овербукинг (резервируются только реально исполнимые слоты каждого тира)
            locked_usdt = paper.effective_locked_usdt()
            free_usdt = max(0.0, paper.usdt - locked_usdt)

            if free_usdt < min_usdt_order:
                if self._try_rotate_order(cand, grade, tickers, reason_tag="нехватка свободного USDT"):
                    locked_usdt = paper.effective_locked_usdt()
                    free_usdt = max(0.0, paper.usdt - locked_usdt)

            size_base = buy_size(
                equity, cand["score"], thr, cand["liquidity"], free_usdt,
                entry_mode=entry_mode, grade=grade
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
