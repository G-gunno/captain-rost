def fmt_price(p) -> str:
    """Красивая цена: адаптивное число знаков."""
    p = float(p)
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.6f}"
    return f"{p:.8f}"


def fmt_usdt(x) -> str:
    return f"{float(x):.2f}"


def fmt_pct(x) -> str:
    return f"{float(x):+.2f}%"


def fmt_sym(s) -> str:
    if s.endswith("USDT"):
        return s[:-4] + "/USDT"
    return s
