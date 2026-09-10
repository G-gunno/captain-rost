import os
from bot.news.cmc import TIER_EMOJI

def fmt_price(p) -> str:
    p = float(p)
    if p >= 1000: return f"{p:,.2f}"
    if p >= 1: return f"{p:.4f}"
    if p >= 0.01: return f"{p:.6f}"
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
    return "🟢" if x > 0.05 else ("🔴" if x < -0.05 else "🟡")

def weight_emoji(v) -> str:
    return "🔥" if v >= 1.1 else ("🟢" if v >= 0.9 else ("🟡" if v >= 0.7 else "🔻"))

def funding_line(x) -> str:
    return f"\n🏦 в накопления {usd(x)}" if x > 0 else ""

def corr_txt(d) -> str:
    v = d.get("corr") if isinstance(d, dict) else None
    return f" · ₿ {v:.2f}" if v is not None else ""

def pair_html(sym: str, data_obj: dict) -> str:
    kind_tag = "🛰" if data_obj.get("kind") == "satellite" else "🏛"
    
    emode = data_obj.get("entry_mode", "")
    if emode == "reversal": mode_tag = "🧲"
    elif emode == "rocket" or data_obj.get("is_momentum"): mode_tag = "🚀"
    else: mode_tag = "🏹"
        
    tier = data_obj.get("tier")
    em = TIER_EMOJI.get(tier, "") if tier else ""
    sector = data_obj.get("sector") or "Other"
    
    base_sym = sym[:-4] if sym.endswith("USDT") else sym
    public_url = os.getenv("RENDER_EXTERNAL_URL", "https://captain-rost-bot.onrender.com")
    chart_url = f"{public_url}/chart?symbol={base_sym}USDT"
    
    return f"{mode_tag} {kind_tag} <a href='{chart_url}'><b>{base_sym}</b></a>{' ' + em if em else ''} · <i>{sector}</i>"
