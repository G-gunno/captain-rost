import os
from bot.news.cmc import TIER_EMOJI

# ===== ТОЛКОВЫЙ СЛОВАРЬ ЭМОДЗИ (EMOJI LEXICON) =====
SL_STATE_EMOJI = {
    "SHIELD": "🛡📉",          # Первичный стоп от слива
    "BREAKEVEN": "🛡🔒",       # Безубыток (перенос в ноль / защиту)
    "PROFIT_LOCK": "🛡💼",     # Профит-лок (фиксация накопленной прибыли ДО TP1)
    "TRAILING_RUNNER": "🛡📈", # Трейлинг (динамический коридор ПОСЛЕ TP1)
    "ABOVE_TP1": "🛡🔥",       # Стоп выше TP1 (гарантированный супер-профит выше 1-й цели)
}

MODE_EMOJI = {
    "rocket": "🚀",
    "reversal": "🧲",
    "sniper": "🏹",
}


def sl_emoji(state: str) -> str:
    return SL_STATE_EMOJI.get(state or "SHIELD", "🛡📉")


def mode_emoji(data_obj: dict) -> str:
    if not isinstance(data_obj, dict):
        return "🏹"
    emode = data_obj.get("entry_mode", "")
    if emode == "reversal":
        return "🧲"
    if emode == "rocket" or data_obj.get("is_momentum"):
        return "🚀"
    return "🏹"


def grade_badge(data_obj: dict) -> str:
    if isinstance(data_obj, dict) and data_obj.get("grade") == "A+":
        return " [💎]"
    return ""


def fmt_price(p) -> str:
    p = float(p)
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.6f}"
    return f"{p:.8f}"


def usd(x) -> str:
    return f"${float(x):,.2f}"


def fmt_usdt(x) -> str:
    return f"{float(x):.2f}"


def fmt_pct(x) -> str:
    return f"{float(x):+.2f}%"


def fmt_sym(s) -> str:
    return s[:-4] + "/USDT" if s.endswith("USDT") else s


def pnl_emoji(x) -> str:
    return "🟢" if x > 0.25 else ("🔴" if x < -0.15 else "🟡")


def weight_emoji(v) -> str:
    return "🔥" if v >= 1.1 else ("🟢" if v >= 0.9 else ("🟡" if v >= 0.7 else "🔻"))


def funding_line(x) -> str:
    return f"\n🏦 в накопления {usd(x)}" if x > 0 else ""


def corr_txt(d) -> str:
    v = d.get("corr") if isinstance(d, dict) else None
    return f" · ₿ {v:.2f}" if v is not None else ""


def format_coin_compact(sym: str, data_obj: dict = None) -> str:
    """Компактный тег монеты для уведомлений SL: 🧲LINK🐋"""
    data_obj = data_obj or {}
    m_tag = mode_emoji(data_obj)
    tier = data_obj.get("tier")
    t_em = TIER_EMOJI.get(tier, "") if tier else ""
    base_sym = sym[:-4] if sym.endswith("USDT") else sym
    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")
    chart_url = f"{public_url}/chart?symbol={base_sym}USDT"
    return f"{m_tag}<a href='{chart_url}'><b>{base_sym}</b></a>{t_em}"


def format_coin(sym: str, data_obj: dict = None) -> str:
    """Стандартный тег монеты с сектором: 🧲 LINK 🐋 · DeFi"""
    data_obj = data_obj or {}
    m_tag = mode_emoji(data_obj)
    tier = data_obj.get("tier")
    em = TIER_EMOJI.get(tier, "") if tier else ""
    sector = data_obj.get("sector") or "Other"

    base_sym = sym[:-4] if sym.endswith("USDT") else sym
    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")
    chart_url = f"{public_url}/chart?symbol={base_sym}USDT"

    return f"{m_tag} <a href='{chart_url}'><b>{base_sym}</b></a>{' ' + em if em else ''} · <i>{sector}</i>"


pair_html = format_coin
