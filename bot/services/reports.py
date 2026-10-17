from datetime import datetime, timedelta
import time
from collections import defaultdict

from bot.exchange.market_data import market_data
from bot.exchange.paper_exchange import paper
from bot.strategy.learner import learner, is_neutral_be
from bot.news.cmc import TIER_EMOJI
from bot.utils.format import (
    format_coin, sl_emoji, grade_badge, mode_emoji,
    usd, fmt_price, fmt_pct, pnl_emoji
)

PERIODS = {
    "daily": ("#Дневной_отчёт", 1),
    "weekly": ("#Недельный_отчёт", 7),
    "monthly": ("#Месячный_отчёт", 30),
}


def _fmt_ts(ts: int, tz) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(int(ts), tz).strftime("%H:%M")


async def build_trades_audit_24h(tz) -> str:
    """
    Генерирует подробный табличный аудит всех сделок, монет и открытых позиций за последние 24 часа.
    Прикрепляется отдельным файлом при вызове /chart и /log.
    """
    prices = await market_data.get_tickers()
    eq = paper.equity(prices)
    now = datetime.now(tz)
    cutoff = int(time.time()) - 86400

    locked_usdt = sum(o["qty"] * o["price"] for o in paper.orders)
    avail_usdt = max(0.0, paper.usdt - locked_usdt)
    metrics_24h = paper.get_metrics(prices, hours=24)
    a_stats = learner.get_grade_a_stats()

    closed_24h = [
        r for r in paper.realized
        if not r.get("partial") and r.get("time", 0) >= cutoff
    ]
    closed_24h.sort(key=lambda x: x.get("time", 0))

    be_trades = [r for r in closed_24h if r.get("is_be") or is_neutral_be(r.get("pnl_pct", 0.0))]
    decisive = [r for r in closed_24h if r not in be_trades]
    wins = [r for r in decisive if r["pnl"] > 0]
    losses = [r for r in decisive if r["pnl"] <= 0]
    clean_wr = (len(wins) / len(decisive) * 100) if decisive else 0.0

    out = []
    out.append("========================================================================================================================")
    out.append(f"📊 КАПИТАН РОСТ — ПОЛНЫЙ АУДИТ СДЕЛОК И МОНЕТ ЗА 24 ЧАСА ({now.strftime('%Y-%m-%d %H:%M %Z')})")
    out.append("========================================================================================================================")
    out.append(
        f"Капитал: {usd(eq)} | Доступно: {usd(avail_usdt)} | В ордерах: {usd(locked_usdt)} | Накопления: {usd(paper.funding)}\n"
        f"Сделок (24ч): {len(closed_24h)} (✅ Побед: {len(wins)} | ❌ Стопов: {len(losses)} | 🛡🔒 БУ: {len(be_trades)} | 🔥 TP1: {metrics_24h['partial_count']})\n"
        f"Чистый Winrate: {clean_wr:.1f}% | PF: {metrics_24h['profit_factor'] or 0:.2f} | Max DD: {metrics_24h['max_drawdown_pct']:.1f}% | PnL 24ч: {usd(metrics_24h['total_pnl'])}\n"
        f"Сетапы [💎] A+: WR {a_stats['wr']:.0%} ({a_stats['decisive']} рез. + {a_stats['be']} БУ) | Вес доверия: x{a_stats['mult']:.2f} (серия {a_stats['streak']:+d})\n"
        f"Текущий режим риска ИИ: {learner.current_risk_mode} (строгость {learner.threshold_adj:+.2f})"
    )
    out.append("")

    # -------------------------------------------------------------------------
    # ТАБЛИЦА 1: ДЕТАЛЬНЫЙ ХРОНОЛОГИЧЕСКИЙ ЖУРНАЛ ВСЕХ СДЕЛОК ЗА 24 ЧАСА
    # -------------------------------------------------------------------------
    out.append("1. ДЕТАЛЬНАЯ ХРОНОЛОГИЧЕСКАЯ ТАБЛИЦА ВСЕХ ЗАКРЫТЫХ СДЕЛОК (24Ч)")
    out.append("-" * 120)
    out.append(
        "| №  | Монета (Тир/Сектор) | Ордер / Вход / Выход | Стратегия и Грейд | Рынок / Режим | Скор/Порог | Валидация 1H (48ч) и ₿-корр | Почему решили купить (Триггеры и ключи) | Вход -> Выход (TP / SL) | Итог PnL (% / $ / Тип) |"
    )
    out.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")

    if closed_24h:
        for idx, t in enumerate(closed_24h, 1):
            sym_base = t["symbol"][:-4] if t["symbol"].endswith("USDT") else t["symbol"]
            t_em = TIER_EMOJI.get(t.get("tier"), "🐭")
            sec = t.get("sector", "Other")
            m_em = mode_emoji(t)
            emode = t.get("entry_mode", "sniper")
            g_str = "[💎] A+" if t.get("grade") == "A+" else "[B]"
            reg_str = (t.get("regime") or "neutral").upper()
            rmode = t.get("risk_mode", "NORMAL")

            t_ord = _fmt_ts(t.get("order_created"), tz)
            t_in = _fmt_ts(t.get("entry_time"), tz)
            t_out = _fmt_ts(t.get("time"), tz)

            sc = t.get("score", 0.0)
            thr = t.get("thr", 0.0)
            sc_str = f"{sc:.1f}/{thr:.2f}" if sc > 0 else "—"

            g1h = t.get("gate_1h") or "1H_OK"
            r1h = t.get("reason_1h") or "—"
            corr_v = t.get("corr")
            corr_s = f"₿ {corr_v:.2f}" if corr_v is not None else ""
            info_1h = f"{g1h} ({r1h}) {corr_s}".strip()

            reasons_list = t.get("reasons") or t.get("reason_keys") or []
            why_str = "; ".join(str(x) for x in reasons_list[:5]) if reasons_list else "архивная сделка"

            ep = t.get("entry_price")
            xp = t.get("exit_price")
            itp = t.get("initial_tp")
            isl = t.get("initial_sl")
            if ep and xp:
                prices_str = f"${fmt_price(ep)} -> ${fmt_price(xp)}"
                if itp and isl:
                    prices_str += f" (TP ${fmt_price(itp)} / SL ${fmt_price(isl)})"
            else:
                prices_str = "—"

            res_icon = "🛡🔒" if (t.get("is_be") or is_neutral_be(t["pnl_pct"])) else ("✅" if t["pnl"] > 0 else "❌")
            tp1_mark = " + 🔥TP1" if t.get("tp1_done") else ""
            res_str = f"{res_icon} {t['pnl_pct']:+.2f}% ({usd(t['pnl'])}) · {t.get('exit_type', '')}{tp1_mark}"

            out.append(
                f"| {idx} | {sym_base} {t_em} ({sec}) | {t_ord} / {t_in} / {t_out} | {m_em} {emode} {g_str} | "
                f"{reg_str} / {rmode} | {sc_str} | {info_1h} | {why_str} | {prices_str} | {res_str} |"
            )
    else:
        out.append("| — | За последние 24 часа закрытых сделок нет | — | — | — | — | — | — | — | — |")

    out.append("")

    # -------------------------------------------------------------------------
    # ТАБЛИЦА 2: СВОДНЫЙ РЕЕСТР ПО ВСЕМ МОНЕТАМ ЗА 24 ЧАСА (ОТ ЛУЧШИХ К ХУДШИМ)
    # -------------------------------------------------------------------------
    out.append("2. СВОДНЫЙ РЕЕСТР ПО ВСЕМ МОНЕТАМ ЗА 24 ЧАСА (СОРТИРОВКА ПО СУММАРНОМУ PNL)")
    out.append("-" * 120)
    out.append(
        "| Монета | Сектор / Тир | Сделок | Все закрытые сделки за 24ч (Стратегия · Грейд · Результат · Выход) | Открытая позиция сейчас | Суммарный PnL (% / $) |"
    )
    out.append("| :--- | :--- | :---: | :--- | :--- | :--- |")

    by_coin = defaultdict(list)
    for t in closed_24h:
        by_coin[t["symbol"]].append(t)
    for sym in paper.positions:
        by_coin.setdefault(sym, [])

    coin_rows = []
    for sym, t_list in by_coin.items():
        sum_pct = sum(x["pnl_pct"] for x in t_list)
        sum_usd = sum(x["pnl"] for x in t_list)
        sample = t_list[-1] if t_list else paper.positions.get(sym, {})
        sec = sample.get("sector", "Other")
        t_em = TIER_EMOJI.get(sample.get("tier"), "🐭")

        trades_desc = []
        for x in t_list:
            ic = "🛡🔒" if (x.get("is_be") or is_neutral_be(x["pnl_pct"])) else ("✅" if x["pnl"] > 0 else "❌")
            gb = "[💎]" if x.get("grade") == "A+" else ""
            trades_desc.append(f"{ic} {mode_emoji(x)}{gb} {x['pnl_pct']:+.2f}% ({x.get('exit_type', '')})")
        trades_str = " · ".join(trades_desc) if trades_desc else "—"

        pos = paper.positions.get(sym)
        if pos:
            last_p = prices.get(sym, {}).get("last", 0)
            p_pct = (last_p - pos["avg"]) / pos["avg"] * 100 if pos.get("avg") and last_p else 0.0
            pos_str = f"{mode_emoji(pos)}{grade_badge(pos)} {p_pct:+.2f}% ({sl_emoji(pos.get('sl_state'))})"
        else:
            pos_str = "—"

        coin_rows.append((sym, sec, t_em, len(t_list), trades_str, pos_str, sum_pct, sum_usd))

    coin_rows.sort(key=lambda r: r[6], reverse=True)
    for sym, sec, t_em, cnt, trades_str, pos_str, sum_pct, sum_usd in coin_rows:
        sym_base = sym[:-4] if sym.endswith("USDT") else sym
        tot_ic = "🟢" if sum_pct > 0.25 else ("🔴" if sum_pct < -0.15 else "🟡")
        out.append(f"| {sym_base} | {sec} {t_em} | {cnt} | {trades_str} | {pos_str} | {tot_ic} {sum_pct:+.2f}% ({usd(sum_usd)}) |")

    out.append("")

    # -------------------------------------------------------------------------
    # ТАБЛИЦА 3: ТЕКУЩИЕ ОТКРЫТЫЕ ПОЗИЦИИ И АКТИВНЫЕ ЛИМИТНЫЕ ОРДЕРА
    # -------------------------------------------------------------------------
    out.append(f"3. ТЕКУЩИЕ ОТКРЫТЫЕ ПОЗИЦИИ ({len(paper.positions)}) И ЛИМИТНЫЕ ОРДЕРА ({len(paper.orders)})")
    out.append("-" * 120)
    out.append(
        "| Статус | Монета | Стратегия и Грейд | Сумма $ | Вход -> Текущая (PnL %) | TP / SL (Статус стопа) | Скор/Порог | 1H Валидация (48ч) и ₿ | Причины входа |"
    )
    out.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")

    for sym, pos in sorted(paper.positions.items()):
        sym_base = sym[:-4] if sym.endswith("USDT") else sym
        last_p = prices.get(sym, {}).get("last", 0)
        val = pos["qty"] * last_p
        p_pct = (last_p - pos["avg"]) / pos["avg"] * 100 if pos.get("avg") and last_p else 0.0
        sc_s = f"{pos.get('score', 0):.1f}/{pos.get('thr', 0):.2f}" if pos.get("score") else "—"
        r1h = f"{pos.get('gate_1h', '')} ({pos.get('reason_1h', '')}) ₿{pos.get('corr', 0) or 0:.2f}"
        why_p = "; ".join(pos.get("reasons", [])[:4]) or ", ".join(pos.get("reason_keys", []))
        out.append(
            f"| 📦 ПОЗИЦИЯ | {sym_base} ({pos.get('sector', 'Other')}) | {mode_emoji(pos)} {pos.get('entry_mode', 'sniper')}{grade_badge(pos)} | "
            f"{usd(val)} | ${fmt_price(pos['avg'])} -> ${fmt_price(last_p)} ({p_pct:+.2f}%) | "
            f"🎯 ${fmt_price(pos['tp'])} / {sl_emoji(pos.get('sl_state'))} ${fmt_price(pos['sl'])} | {sc_s} | {r1h} | {why_p} |"
        )

    for o in paper.orders:
        sym = o["symbol"]
        sym_base = sym[:-4] if sym.endswith("USDT") else sym
        last_p = prices.get(sym, {}).get("last", 0)
        val = o["qty"] * o["price"]
        dist = (o["price"] - last_p) / last_p * 100 if last_p > 0 else 0.0
        sc_s = f"{o.get('score', 0):.1f}/{o.get('thr', 0):.2f}" if o.get("score") else "—"
        r1h = f"{o.get('gate_1h', '')} ({o.get('reason_1h', '')}) ₿{o.get('corr', 0) or 0:.2f}"
        why_o = "; ".join(o.get("reasons", [])[:4]) or ", ".join(o.get("reason_keys", []))
        out.append(
            f"| 📋 ОРДЕР | {sym_base} ({o.get('sector', 'Other')}) | {mode_emoji(o)} {o.get('entry_mode', 'sniper')}{grade_badge(o)} | "
            f"{usd(val)} | Лимитка ${fmt_price(o['price'])} ({dist:+.2f}% от ${fmt_price(last_p)}) | "
            f"🎯 ${fmt_price(o['tp'])} / 🛡📉 ${fmt_price(o['sl'])} | {sc_s} | {r1h} | {why_o} |"
        )

    return "\n".join(out)


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

    trades = [
        r for r in paper.realized
        if not r.get("partial") and r.get("time", 0) >= start_ts
    ]

    be_trades = [r for r in trades if r.get("is_be") or is_neutral_be(r.get("pnl_pct", 0.0))]
    decisive = [r for r in trades if r not in be_trades]
    wins = [r for r in decisive if r["pnl"] > 0]
    losses = [r for r in decisive if r["pnl"] <= 0]
    total_pnl = sum(r["pnl"] for r in trades)
    clean_wr = (len(wins) / len(decisive) * 100) if decisive else 0.0

    locked_usdt = sum(o["qty"] * o["price"] for o in paper.orders)
    avail_usdt = max(0.0, paper.usdt - locked_usdt)
    free_pct = avail_usdt / eq * 100 if eq else 0

    lines = [f"📅 <b>{title}</b> 🕘", ""]
    lines.append(f"💰 Свободно: <b>{usd(avail_usdt)}</b> ({free_pct:.0f}%)")
    if locked_usdt > 0:
        lines.append(f"🔒 В ордерах: <b>{usd(locked_usdt)}</b>")
    lines.append(f"🏦 Накопления: <b>{usd(paper.funding)}</b>")
    lines.append(f"📈 Капитал: <b>{usd(eq)}</b>")
    lines.append("")

    history = [h for h in getattr(paper, "market_history", []) if h.get("ts", 0) >= start_ts]
    if history:
        total_h = len(history)
        r_counts, m_counts = {}, {}
        for h in history:
            r, m = h.get("regime", "neutral"), h.get("mode", "NORMAL")
            r_counts[r] = r_counts.get(r, 0) + 1
            m_counts[m] = m_counts.get(m, 0) + 1

        r_map = {"bull": "🟢", "neutral": "🟡", "bear": "🔴"}
        m_map = {"NORMAL": "🟢 NORMAL", "CAUTIOUS": "🟡 CAUTIOUS", "STRICT": "🔴 STRICT", "AGGRESSIVE": "🚀 AGGRESSIVE"}

        r_str = " · ".join(f"{r_map.get(k, k)} {v/total_h*100:.0f}%" for k, v in sorted(r_counts.items(), key=lambda x: x[1], reverse=True))
        m_str = " · ".join(f"{m_map.get(k, k)} {v/total_h*100:.0f}%" for k, v in sorted(m_counts.items(), key=lambda x: x[1], reverse=True))

        lines.append(f"🌍 <b>Фон:</b> 🧭 {r_str} | 🎚 {m_str}")
        lines.append("")

    lines.append(
        f"📊 <b>Сделки ({len(trades)}):</b> ✅ {len(wins)} · ❌ {len(losses)} · 🛡🔒 {len(be_trades)} "
        f"<i>(чистый WR {clean_wr:.0f}%)</i>"
    )
    lines.append(f"💵 PnL: {pnl_emoji(total_pnl)} <b>{usd(total_pnl)}</b>")

    if trades:
        exit_counts = {}
        for r in trades:
            et = r.get("exit_type", "🛡📉")
            exit_counts[et] = exit_counts.get(et, 0) + 1
        ex_str = " · ".join(f"{k}: {v}" for k, v in sorted(exit_counts.items(), key=lambda kv: kv[1], reverse=True))
        lines.append(f"🚪 <b>Выходы:</b> {ex_str}")

    if decisive:
        best = max(decisive, key=lambda r: r["pnl_pct"])
        worst = min(decisive, key=lambda r: r["pnl_pct"])
        lines.append(f"🏆 {format_coin(best['symbol'], best)}{grade_badge(best)} · {pnl_emoji(best['pnl_pct'])} {best['pnl_pct']:+.1f}% ({best.get('exit_type', '')})")
        lines.append(f"📉 {format_coin(worst['symbol'], worst)}{grade_badge(worst)} · {pnl_emoji(worst['pnl_pct'])} {worst['pnl_pct']:+.1f}% ({worst.get('exit_type', '')})")
    lines.append("")

    a_trades = [r for r in trades if r.get("grade") == "A+"]
    a_stats_all = learner.get_grade_a_stats()
    lines.append("💎 <b>Элитные сетапы [💎]</b>")
    if a_trades:
        a_be = [r for r in a_trades if r.get("is_be") or is_neutral_be(r.get("pnl_pct", 0.0))]
        a_dec = [r for r in a_trades if r not in a_be]
        a_wins = [r for r in a_dec if r["pnl"] > 0]
        a_wr = (len(a_wins) / len(a_dec) * 100) if a_dec else 0.0
        a_pnl = sum(r["pnl"] for r in a_trades)
        lines.append(
            f"   За период: {len(a_trades)} (✅ {len(a_wins)} / ❌ {len(a_dec)-len(a_wins)} / 🛡🔒 {len(a_be)}) "
            f"· WR {a_wr:.0f}% · {usd(a_pnl)}"
        )
    lines.append(
        f"   ИИ-доверие [💎]: <b>×{a_stats_all['mult']:.2f}</b> "
        f"(серия {a_stats_all['streak']:+d} · общий WR {a_stats_all['wr']:.0%} за {a_stats_all['decisive']} сдел.)"
    )
    lines.append("")

    sector_agg = {}
    for r in decisive:
        sector_agg.setdefault(r.get("sector", "Other"), []).append(r["pnl_pct"])
    lines.append("🧭 <b>Сектора</b>")
    if sector_agg:
        rows = [(s, len(v), sum(v) / len(v)) for s, v in sector_agg.items()]
        rows.sort(key=lambda x: x[2], reverse=True)
        for s, cnt, avg in rows[:6]:
            lines.append(f"   {pnl_emoji(avg)} <i>{s}</i> · {cnt} · {avg:+.2f}%")
    else:
        lines.append("   <i>(нет результативных сделок)</i>")
    lines.append("")

    if paper.positions:
        lines.append(f"📦 <b>Позиции ({len(paper.positions)})</b>")
        for sym, pos in paper.positions.items():
            last = prices.get(sym, {}).get("last", 0)
            pnl_pct = (last - pos["avg"]) / pos["avg"] * 100 if pos["avg"] and last else 0
            tp_tag = " · 🔥 TP1 · TP🏃🏻‍➡️" if pos.get("tp1_done") else ""
            sl_tag = sl_emoji(pos.get("sl_state", "SHIELD"))
            lines.append(
                f"   {format_coin(sym, pos)}{grade_badge(pos)} · "
                f"{pnl_emoji(pnl_pct)} {fmt_pct(pnl_pct)} · {sl_tag} {fmt_price(pos['sl'])}{tp_tag}"
            )
    else:
        lines.append("📦 <b>Позиции</b>: нет")

    return "\n".join(lines)
