# Формат: (Equity, Floor (Base Min), Ceiling (Base Max))
TIERS = [
    (150, 10, 25),
    (250, 15, 40),
    (500, 20, 75),
    (1000, 30, 120),
    (2500, 50, 250),
    (5000, 100, 500),
    (10000, 200, 1000),
]

def tier_limits(equity):
    """Возвращает Floor (Base Min) и Ceiling (Base Max) для текущего капитала."""
    for bound, mn, mx in TIERS:
        if equity <= bound:
            return mn, mx
    return max(200.0, equity * 0.02), equity * 0.10

def portfolio_limits(equity):
    """Возвращает максимальное количество активных позиций: (Лимит_на_Сектор, Лимит_Other)."""
    if equity <= 150: return 2, 2
    if equity <= 250: return 3, 2
    if equity <= 500: return 4, 3
    if equity <= 1000: return 6, 8
    if equity <= 2500: return 8, 10
    if equity <= 5000: return 10, 12
    return 12, 15

def buy_size(equity, score, thr, liquidity, free_usdt, kind="core", is_momentum=False, size_multiplier=1.0):
    """
    Новая логика сайзинга (Risk-Free Sizing).
    Отвязывает размер позиции от Stop-Loss. Размер зависит исключительно от силы сигнала (Score).
    Никаких микро-покупок: жесткий Floor для любой сделки.
    """
    base_min, base_max = tier_limits(equity)
    
    # 1. Расчет базового размера в зависимости от типа монеты
    if kind == "satellite":
        # У сателлитов Максимум = 3x от Минимума
        sat_max = base_min * 3.0
        score_range = 10.0 - thr
        if score_range <= 0:
            strength = 0.0
        else:
            strength = max(0.0, min(1.0, (score - thr) / score_range))
        size = base_min + (sat_max - base_min) * strength
    else:
        # У Core-монет диапазон от Base Min до Base Max
        score_range = 10.0 - thr
        if score_range <= 0:
            strength = 0.0
        else:
            strength = max(0.0, min(1.0, (score - thr) / score_range))
        size = base_min + (base_max - base_min) * strength

    # 2. Штраф за низкую ликвидность (сбрасываем до Floor)
    if liquidity < 500_000:
        size = base_min

    # 3. Применение множителя режима входа (Ракета vs Снайпер)
    if is_momentum:
        # Ракета умножается на динамический коэффициент Теневого журнала
        size = size * size_multiplier
    else:
        # Снайпер получает бонус +10% за безопасность лимитного ордера
        size = size * 1.1

    # 4. Жесткие границы (Floor и Ceiling)
    # Запрещаем размеру опускаться ниже Базового Минимума (спасает от пыли на балансе)
    # Запрещаем размеру превышать Базовый Максимум (или Sat Max для сателлитов)
    max_allowed = (base_min * 3.0) if kind == "satellite" else base_max
    size = max(base_min, min(size, max_allowed))

    # 5. Здравый смысл и свободный баланс
    # Нельзя покупать больше, чем есть USDT, и больше 20% от всего депо в 1 сделку.
    size = min(size, free_usdt * 0.95, equity * 0.20)
    
    return round(size, 2)
