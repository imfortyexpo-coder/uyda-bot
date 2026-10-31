"""
Проверки качества данных таблицы — находят строки, которые, скорее всего, введены с ошибкой.
Бот таблицу не меняет: он только показывает, где посмотреть. Логика без Telegram.

Работает с «грузами» из categories.build_index (части одного груза уже объединены).
"""

import statistics

import categories as ct

# Пороги (можно менять)
OVERHEAD_PCT_MAX = 150.0     # расходы больше 150% цены завода — почти всегда ошибка ввода
OVERHEAD_PCT_MIN = 3.0       # меньше 3% — скорее всего, не внесены расходы
RATE_DEVIATION = 0.30        # курс отличается от обычного курса того же месяца больше чем на 30%

# kind -> (иконка, название, пояснение)
KINDS: dict[str, tuple[str, str, str]] = {
    "bg":    ("⚖️", "BG ≠ расчёт бота",
              "Итог в столбце BG отличается от суммы цены завода и всех расходов больше чем на max($5; 0,5%)."),
    "rate":  ("💱", "Подозрительный курс",
              f"Курс S (или H) отличается от обычного курса того же месяца больше чем на {RATE_DEVIATION:.0%}. "
              "Из-за ошибки в курсе неверно пересчитываются все расходы в сумах."),
    "high":  ("📈", f"Расходы больше {OVERHEAD_PCT_MAX:.0f}% цены завода",
              "Обычно это сумма, вписанная не в тот столбец, или сумы в столбце долларов."),
    "low":   ("📉", f"Расходы меньше {OVERHEAD_PCT_MIN:.0f}% цены завода",
              "Скорее всего, внесены не все расходы."),
    "ac":    ("🧾", "Таможня AC ≠ Y+Z+AA+AB",
              "Итог таможенных платежей не равен сумме сбора, пошлины, акциза и НДС."),
    "norate": ("ℹ️", "Нет курса", "Не заполнены курс S и курс ГТД H — себестоимость не посчитана."),
    "regime": ("📜", "Метка режима ≠ содержимое",
               "В названии стоит ИМ70 или ИМ74 (временное хранение / таможенный склад), но в этой части груза есть "
               "цена завода — то есть это полная декларация. Скорее всего, ошибка в названии: проверьте режим."),
    "nofactory": ("🏭", "Нет цены завода",
                  "Ни в одной части груза не заполнен столбец N — себестоимость не посчитана."),
}
KIND_ORDER = list(KINDS)


def _f(x: float, decimals: int = 0, sign: bool = False) -> str:
    """Число с пробелами между разрядами: 1 234 567 (и знак +/−, если sign)."""
    t = f"{x:{'+' if sign else ''},.{decimals}f}".replace(",", " ").replace(".", ",")
    return t.replace("-", "−")


def _rate(rec: dict) -> float | None:
    r = rec.get("calc_rate_used") or rec.get("market_usd_rate") or rec.get("usd_rate_gtd")
    return r if isinstance(r, (int, float)) and r > 0 else None


def month_rates(cargos: list[dict]) -> dict[str, float]:
    """Обычный (медианный) курс по месяцам ГТД — по всем строкам таблицы."""
    by_month: dict[str, list[float]] = {}
    for c in cargos:
        for p in c["parts"]:
            d, r = p.get("gtd_date"), _rate(p)
            if d and r:
                by_month.setdefault(d.strftime("%Y-%m"), []).append(r)
    return {m: statistics.median(v) for m, v in by_month.items() if len(v) >= 3}


def problems(cargo: dict, rates: dict[str, float]) -> dict[str, str]:
    """{вид проблемы: короткая подпись с цифрами} для одного груза."""
    out: dict[str, str] = {}
    agg, parts = cargo["agg"], cargo["parts"]

    d = ct.bg_diff(cargo)
    if d is not None:
        out["bg"] = f"BG − расчёт = {_f(d, sign=True)} $"

    for p in parts:
        r, dt = _rate(p), p.get("gtd_date")
        if r and dt:
            usual = rates.get(dt.strftime("%Y-%m"))
            if usual and abs(r / usual - 1) > RATE_DEVIATION:
                out["rate"] = f"курс {_f(r)}, обычно {_f(usual)}"
                break

    pct = agg.get("calc_overhead_pct")
    if isinstance(pct, (int, float)):
        if pct > OVERHEAD_PCT_MAX:
            out["high"] = f"расходы {_f(pct)}% цены завода"
        elif pct < OVERHEAD_PCT_MIN:
            out["low"] = f"расходы {_f(pct, 1)}% цены завода"

    diff = [p["calc_customs_check_diff"] for p in parts if isinstance(p.get("calc_customs_check_diff"), (int, float))]
    if diff:
        out["ac"] = f"разница {_f(sum(diff), sign=True)} сум"

    for p in parts:
        if ct.IM_WAREHOUSE.search(p.get("title", "")) and p.get("factory_usd"):
            out["regime"] = f"метка {'/'.join(sorted(ct.regime_codes(p['title']) & {'ИМ70', 'ИМ74'}))}, но есть цена завода"
            break

    if not any(_rate(p) for p in parts):
        out["norate"] = "нет курса S и H"
    elif not any(p.get("factory_usd") for p in parts):
        out["nofactory"] = "нет столбца N"
    return out


def scan(cargos: list[dict]) -> dict[str, list[tuple[dict, str]]]:
    """{вид: [(груз, подпись)]} — новые грузы сверху (в том порядке, что и cargos)."""
    rates = month_rates(cargos)
    res: dict[str, list[tuple[dict, str]]] = {k: [] for k in KIND_ORDER}
    for c in cargos:
        for kind, label in problems(c, rates).items():
            res[kind].append((c, label))
    return res
