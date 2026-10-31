"""
Разбивка расходов груза по валютам (как платили: $ или сум), по видам расходов и по способу оплаты
(наличные / перечисление). Правила те же, что в sheet.compute(): суммы в $ совпадают с «Расходы итого».
Работает с одной строкой груза или со списком частей / грузов за период.
"""

import config as cfg

USD, UZS = "USD", "UZS"
CASH, TRANSFER, UNKNOWN = "cash", "transfer", ""

# Виды расходов в порядке показа
TYPES: list[tuple[str, str]] = [
    ("bank", "Банк (перевод оплаты)"),
    ("customs", "Таможенные платежи"),
    ("certs", "Сертификаты и разрешения"),
    ("logistics", "Логистика"),
    ("inspection", "Досмотр и оформление"),
    ("agent", "Процент завода / агент"),
    ("loading", "ПРР (погрузка-разгрузка)"),
    ("other", "Прочие расходы"),
]
TYPE_LABEL = dict(TYPES)


def _g(r: dict, key: str) -> float:
    v = r.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def items(r: dict) -> list[tuple[str, str, str, float, str]]:
    """Статьи одной строки груза: (вид, валюта, оплата, сумма в валюте, ключ столбца)."""
    out: list[tuple[str, str, str, float, str]] = []

    def add(kind: str, cur: str, pay: str, key: str, value: float | None = None):
        v = _g(r, key) if value is None else value
        if v:
            out.append((kind, cur, pay, v, key))

    add("bank", USD, TRANSFER, "paid_goods_usd")

    parts = sum(_g(r, k) for k in ("fee_10", "duty_20", "excise_27", "vat_29"))
    customs = r.get("customs_total")
    add("customs", UZS, UNKNOWN, "customs_total", customs if isinstance(customs, (int, float)) else parts)

    for cash, transfer in (("hyg_cash_uzs", "hyg_transfer_uzs"), ("cert_cash_uzs", "cert_transfer_uzs"),
                           ("eco_cash_uzs", "eco_transfer_uzs"), ("quar_cash_uzs", "quar_transfer_uzs")):
        add("certs", UZS, CASH, cash)
        add("certs", UZS, TRANSFER, transfer)
    add("certs", UZS, UNKNOWN, "expert_uzs")

    add("logistics", USD, CASH, "logistics_cash_usd")
    ax = _g(r, "logistics_transfer")
    if ax:
        add("logistics", UZS if ax > cfg.LOGISTICS_UZS_THRESHOLD else USD, TRANSFER, "logistics_transfer", ax)

    for key in ("inspection_usd", "clearance_usd"):   # в старых строках вписаны в сумах
        v = _g(r, key)
        if v:
            add("inspection", UZS if v > cfg.USD_FIELD_UZS_THRESHOLD else USD, UNKNOWN, key, v)

    add("agent", USD, UNKNOWN, "factory_pct_usd")
    add("loading", UZS, UNKNOWN, "loading_uzs")
    add("other", UZS, UNKNOWN, "other_uzs")
    add("other", UZS, UNKNOWN, "ar_uzs")
    add("other", UZS, CASH, "at_cash_uzs")
    add("other", UZS, TRANSFER, "au_transfer_uzs")
    return out


def breakdown(records: list[dict]) -> dict:
    """Сводит статьи всех строк. Сумы переводятся в $ по курсу своей строки (S, иначе H)."""
    res = {
        "factory_usd": 0.0,
        "paid": {USD: 0.0, UZS: 0.0},          # сколько заплачено в каждой валюте (в этой валюте)
        "uzs_in_usd": 0.0,                     # сумовая часть, пересчитанная в $
        "by_type": {k: {"usd": 0.0, USD: 0.0, UZS: 0.0} for k, _ in TYPES},
        "by_pay": {CASH: 0.0, TRANSFER: 0.0, UNKNOWN: 0.0},   # в $
        "total_usd": 0.0,
        "no_rate_uzs": 0.0,                    # сумы строк без курса — в $ не пересчитаны
        "no_rate_rows": 0,
        "unconfirmed": set(),
        "rates": set(),
    }
    for r in records:
        res["factory_usd"] += _g(r, "factory_usd")
        rate = r.get("market_usd_rate") or r.get("usd_rate_gtd")
        rate = rate if isinstance(rate, (int, float)) and rate > 0 else None
        if rate:
            res["rates"].add(rate)
        unconf = set(r.get("unconfirmed", []))
        row_no_rate = False
        for kind, cur, pay, value, key in items(r):
            res["paid"][cur] += value
            res["by_type"][kind][cur] += value
            if key in unconf:
                res["unconfirmed"].add(kind)
            if cur == USD:
                in_usd = value
            elif rate:
                in_usd = value / rate
                res["uzs_in_usd"] += in_usd
            else:
                res["no_rate_uzs"] += value
                row_no_rate = True
                continue
            res["by_type"][kind]["usd"] += in_usd
            res["by_pay"][pay] += in_usd
            res["total_usd"] += in_usd
        res["no_rate_rows"] += row_no_rate
    return res
