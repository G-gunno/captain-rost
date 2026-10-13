import time
import asyncio
from loguru import logger

from bot.core.event_bus import EventBus
from bot.exchange.paper_exchange import paper
from bot.strategy.sizing import buy_size, portfolio_limits, entry_offset
from bot.strategy.scanner import get_thresholds
from bot.strategy.learner import learner
from bot.strategy.risk import (calc_initial_sl_tp, get_trade_policy, 
                               evaluate_setup_grade, calc_total_open_risk_pct)
from bot.strategy.indicators import get_all_sr_levels
from bot.news.cmc import get_coin_name
from bot.news.rss_news import fetch_news_cache, check_sentiment
from bot.utils.format import pair_html, corr_txt, usd, fmt_price, fmt_pct
from bot.core.state import bot_state

class OrderManagerWorker:
    """Управляет портфелем: контроль совокупного риска, Grade A+ и фильтрация структуры."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self.signal_queue = self.bus.subscribe("SIGNALS_READY", maxsize=2)

    async def run(self):
        logger.info("💼 Order Manager запущен: контроль Portfolio VaR и политик риска...")
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

    async def _process_signals(self, payload: dict):
        candidates = payload["candidates"]
        tickers = payload["tickers"]
        regime = payload["regime"]

        equity = paper.equity(tickers)
        thrs = get_thresholds(regime)
        sec_lim, other_lim = portfolio_limits(equity)
        news_items = await fetch_news_cache()

        risk_mode = getattr(learner, "current_risk_mode", "NORMAL")
        policy = get_trade_policy(risk_mode)

        logger.info(f"💼 OrderManager [{risk_mode}]: кандидатов {len(candidates)}, открытый риск {calc_total_open_risk_pct(paper.positions, paper.orders, equity):.2f}% / {policy['max_portfolio_risk_pct']}%")

        for cand in candidates:
            sym = cand["symbol"]
            base = sym[:-4]

            # 1. Предварительный фильтр новостей
            name = await get_coin_name(base)
            neg, pos_news, mentions, _ = check_sentiment(news_items, [base, name])
            if neg > 0 and neg >= (pos_news * 2) and neg >= (mentions * 0.33):
                logger.info(f"OrderManager: {sym} отклонен -> токсичные новости ({neg}/{mentions})")
                bot_state.set_cooldown(sym, 7200)
                continue

            patterns = cand.get("patterns", [])
            is_accum = "vcp" in patterns
            if bot_state.is_on_cooldown(sym) and not is_accum:
                continue

            if sym in paper.positions or any(o["symbol"] == sym for o in paper.orders):
                continue

            sector = cand.get("sector", "Other")
            tier = cand.get("tier") or "SMALL"
            entry_mode = cand.get("entry_mode", "sniper")
            is_mom = cand.get("is_momentum", False)

            # 2. Общие лимиты диверсификации секторов
            lim = other_lim if sector == "Other" else sec_lim
            sector_count = sum(1 for p in paper.positions.values() if (p.get("sector") or "Other") == sector)
            sector_orders = sum(1 for o in paper.orders if (o.get("sector") or "Other") == sector)
            if (sector_count + sector_orders) >= lim:
                logger.debug(f"OrderManager: {sym} пропущен (лимит сектора {sector}: {lim})")
                continue

            # 3. Барьер качества Grade A+ для режима STRICT / CAUTIOUS
            grade = evaluate_setup_grade(cand)
            if policy["require_grade_a_for_alt"] and entry_mode in ("reversal", "rocket"):
                if grade != "A+":
                    logger.info(f"OrderManager: {sym} пропущен -> режим {risk_mode} требует грейд A+ для {entry_mode} (текущий: {grade})")
                    continue

            thr = thrs.get(entry_mode, 6.0)
            off = entry_offset(cand["score"], thr, regime, cand["atr_pct"], entry_mode, tier=tier, is_accumulation=is_accum)

            t_data = tickers.get(sym, {})
            bid1 = t_data.get("bid1", cand["last"])
            ideal_entry = cand["last"] * (1 + off)
            entry = ideal_entry if is_mom else min(ideal_entry, bid1)

            sup = cand.get("nearest_sup")
            sup_touches = cand.get("sup_touches", 0)
            if sup and sup_touches >= 2 and not is_mom:
                retest_floor = sup * 1.0015
                if cand["last"] >= retest_floor:
                    entry = max(entry, retest_floor)
                    entry = min(entry, bid1)

            a = cand["atr"]
            if a <= 0: continue

            # Поиск актуального сопротивления строго относительно цены входа
            all_res = cand.get("signal_values", {}).get("all_res", [])
            res_above_entry = [r for r in all_res if r > entry * 1.0015]
            entry_res = min(res_above_entry) if res_above_entry else cand.get("nearest_res")
            res_touches = 2 if entry_res else 0

            min_rr = policy["min_rr_sniper"] if entry_mode == "sniper" else policy["min_rr_alt"]

            sl, tp, is_valid, rej_reason = calc_initial_sl_tp(
                symbol=sym, entry=entry, tier=tier, a=a,
                sup=sup, sup_touches=sup_touches,
                res=entry_res, res_touches=res_touches,
                is_mom=is_mom, entry_mode=entry_mode,
                min_rr=min_rr
            )

            if not is_valid:
                logger.info(f"OrderManager: {sym} отклонён -> {rej_reason}")
                continue

            # 4. Бюджетирование совокупного риска (Portfolio VaR)
            sl_dist_pct = (entry - sl) / entry * 100
            if sl_dist_pct <= 0: continue

            trade_risk_budget_pct = policy["risk_per_trade_core"] if entry_mode == "sniper" else policy["risk_per_trade_alt"]
            current_risk_pct = calc_total_open_risk_pct(paper.positions, paper.orders, equity)
            avail_risk_budget_pct = policy["max_portfolio_risk_pct"] - current_risk_pct

            if avail_risk_budget_pct <= 0.10:
                logger.debug(f"OrderManager: {sym} пропущен -> лимит риска {policy['max_portfolio_risk_pct']}% исчерпан")
                continue

            effective_risk_pct = min(trade_risk_budget_pct, avail_risk_budget_pct)
            size_by_risk = (equity * (effective_risk_pct / 100.0)) / (sl_dist_pct / 100.0)

            size_base = buy_size(equity, cand["score"], thr, cand["liquidity"], paper.usdt,
                                 entry_mode=entry_mode)

            size = min(size_base, size_by_risk, paper.usdt * 0.95, equity * 0.10)
            size = round(size, 2)

            if size < 10:
                logger.debug(f"OrderManager: {sym} пропущен (размер {size} < 10 USDT)")
                continue

            qty = size / entry
            order = paper.place_limit_buy(sym, qty, entry, tp=tp, sl=sl, score=cand["score"], reason_keys=cand.get("reason_keys", []))
            
            order.update({
                "sector": sector, "tier": tier, "corr": cand.get("corr"),
                "regime": regime, "is_momentum": is_mom, "entry_mode": entry_mode,
                "nearest_res": entry_res,
                "res_touches": res_touches,
                "nearest_sup": sup,
                "sup_touches": sup_touches,
                "sl_state": "SHIELD",
                "patterns": patterns,
                "grade": grade
            })
            paper.save()

            reasons_html = "<br>".join([f"• {r}" for r in cand.get("reasons", [])])
            paper.log_event(sym, "order_placed", entry, text=reasons_html, mode=entry_mode, tp=tp, sl=sl, sup=sup, res=entry_res)

            tp_pct = (tp - entry) / entry * 100
            sl_pct = (sl - entry) / entry * 100
            grade_badge = f" [Grade {grade}]" if grade == "A+" else ""

            logger.info(f"OrderManager: 🎯 {sym} ОРДЕР {entry_mode}{grade_badge}: entry=${fmt_price(entry)}, TP=${fmt_price(tp)}, SL=${fmt_price(sl)}")

            self._notify(
                f"📋 <b>Ордер</b> · {pair_html(sym, order)}{grade_badge}\n"
                f"💵 {usd(size)} · 📥 {fmt_price(entry)} ({off * 100:+.2f}%){corr_txt(cand)}\n"
                f"🎯 {fmt_price(tp)} ({fmt_pct(tp_pct)}) · 🛡 {fmt_price(sl)} ({fmt_pct(sl_pct)})\n"
                f"⭐ {cand['score']:.1f} · 🧠 {'; '.join(cand['reasons'][:3])}"
            )
