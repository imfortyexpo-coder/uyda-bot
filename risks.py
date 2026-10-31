"""
Управление рисками импорта UYDA (логика без Telegram).

Риск = вероятность (1–5) × влияние (1–5) = балл 1–25.
  🔴 высокий ≥ 15 · 🟠 средний 8–14 · 🟡 низкий 4–7 · 🟢 нет риска
Бот сам находит признаки риска по таблице (по каждой открытой партии и по всему портфелю),
а Директор может добавлять риски вручную. Таблицу бот не меняет.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

# Пороги (переопределяются из config.py)
COST_PCT = 30.0          # расходы выше обычных для этого поставщика больше чем на 30% — риск
COST_MIN_SAMPLES = 5     # «обычные» считаем, если у поставщика есть хотя бы 5 партий с расчётом
SUPPLIER_SHARE = 50.0    # один поставщик даёт больше 50% партий за 12 месяцев — зависимость
COUNTRY_SHARE = 70.0     # одна страна даёт больше 70% партий за 12 месяцев
WAREHOUSE_DAYS = 30      # партия под ИМ74 / ИМ70 без выпуска ИМ40 дольше 30 дней
AGING_DAYS = 30          # открытая партия: прошло больше 30 дней от ГТД
ALERT_SCORE = 15         # с какого балла — немедленное оповещение (🔴)

LEVELS = [(15, "🔴", "высокий"), (8, "🟠", "средний"), (4, "🟡", "низкий"), (0, "🟢", "нет")]

# kind -> (иконка, название, группа, кто отвечает по умолчанию: customs / operator / director / developer / resp)
KINDS: dict[str, tuple[str, str, str, str]] = {
    "cost_high": ("💸", "Расходы выше обычных", "Деньги", "customs"),
    "rate_cbu":  ("🏦", "Курс ГТД ≠ курс ЦБ", "Деньги", "customs"),
    "rate_month": ("💱", "Подозрительный курс", "Деньги", "customs"),
    "nofactory": ("🏭", "Нет цены завода", "Деньги", "customs"),
    "bg":        ("⚖️", "Итог BG не сходится", "Учёт", "customs"),
    "ac":        ("🧾", "Таможня AC ≠ Y+Z+AA+AB", "Учёт", "customs"),
    "yellow":    ("🟡", "Неподтверждённые суммы", "Учёт", "customs"),
    "docs":      ("📄", "Нет счёт-фактур", "Документы", "customs"),
    "sla":       ("⏳", "Срок этапа прошёл", "Сроки", "resp"),
    "aging":     ("📆", "Долго от ГТД до завершения", "Сроки", "resp"),
    "warehouse": ("🏬", "Склад без выпуска ИМ40", "Склад", "customs"),
    "supplier_share": ("🏭", "Зависимость от одного поставщика", "Поставщики", "director"),
    "country_share":  ("🌍", "Зависимость от одной страны", "Поставщики", "director"),
    "supplier_trend": ("📈", "Расходы поставщика растут", "Поставщики", "director"),
    "bot_sheet":  ("🔴", "Таблица не читается", "Работа бота", "developer"),
    "bot_backup": ("🗄", "Нет свежей копии базы", "Работа бота", "developer"),
    "bot_stale":  ("📄", "Файл таблицы давно не обновлялся", "Работа бота", "developer"),
    "manual":     ("✍️", "Ручной риск", "Другое", "director"),
}


def level(score: int) -> tuple[str, str]:
    for lim, icon, name in LEVELS:
        if score >= lim:
            return icon, name
    return "🟢", "нет"


@dataclass
class Signal:
    kind: str
    p: int                      # вероятность 1–5
    i: int                      # влияние 1–5
    why: str                    # объяснение для человека (по-русски)
    ref: str = ""               # к чему относится: c:<id партии>, s:<поставщик>, k:<страна>, bot
    title: str = ""             # о чём риск (название партии / поставщика)
    owner: str = ""             # customs / operator / director / developer

    @property
    def score(self) -> int:
        return self.p * self.i


@dataclass
class CargoRisk:
    ids: list[int]
    title: str
    signals: list[Signal] = field(default_factory=list)

    @property
    def score(self) -> int:
        return max((s.score for s in self.signals), default=0)


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _median(vals: list[float]) -> float | None:
    vals = sorted(vals)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2


def supplier_medians(cargos: list[dict]) -> dict[str, tuple[float, int]]:
    """Поставщик -> (обычная доля расходов к заводу, сколько партий в выборке)."""
    by: dict[str, list[float]] = {}
    for c in cargos:
        pct = _num(c["agg"].get("calc_overhead_pct"))
        if pct is not None and 0 < pct < 300 and _num(c["agg"].get("factory_usd")):
            by.setdefault(c["keys"]["s"][0], []).append(pct)
    return {k: (_median(v), len(v)) for k, v in by.items() if len(v) >= COST_MIN_SAMPLES}


def _days_since(d) -> int | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, date):
        return (date.today() - d).days
    return None


def _first_gtd(c: dict):
    days = [p.get("gtd_date") for p in c["parts"] if p.get("gtd_date") is not None]
    return min(days) if days else None


def cargo_signals(c: dict, ctx: dict) -> list[Signal]:
    """Признаки риска одной открытой партии. ctx — данные бота:
    medians, flags {kind: set(id)}, stage(rid) -> (дней, норма, роль), resp(rid) -> роль."""
    out: list[Signal] = []
    ids = c["ids"]
    rid = ids[0]
    a = c["agg"]
    title = c["title"]
    ref = f"c:{rid}"

    def add(kind, p, i, why, owner=""):
        out.append(Signal(kind, p, i, why, ref, title, owner or KINDS[kind][3]))

    # деньги: расходы выше обычных для этого поставщика
    pct = _num(a.get("calc_overhead_pct"))
    med = ctx["medians"].get(c["keys"]["s"][0])
    if pct is not None and med and med[0]:
        over = (pct - med[0]) / med[0] * 100
        if over >= COST_PCT and pct - med[0] >= 5:
            strong = over >= COST_PCT * 2
            add("cost_high", 4 if strong else 3, 4,
                f"Расходы к цене завода: {pct:.0f}%; обычно у этого поставщика: {med[0]:.0f}% (+{over:.0f}%); "
                f"партий в выборке: {med[1]}")
    flags = ctx["flags"]
    if set(ids) & flags.get("rate", set()):
        add("rate_month", 4, 4, "Курс S или H сильно отличается от обычного курса месяца — пересчёт сумов неверный")
    if set(ids) & flags.get("cbu", set()):
        add("rate_cbu", 4, 3, "Курс H отличается от курса ЦБ на дату ГТД")
    if not any(_num(p.get("factory_usd")) for p in c["parts"]):
        add("nofactory", 4, 3, "Нет цены завода (столбец N) — себестоимость не посчитана")
    if set(ids) & flags.get("bg", set()):
        add("bg", 3, 3, "Итог BG не равен цене завода и всем расходам")
    if set(ids) & flags.get("ac", set()):
        add("ac", 3, 3, "Итог таможни AC не равен Y+Z+AA+AB")
    yellow = sum(len(p.get("unconfirmed", [])) for p in c["parts"])
    if yellow:
        add("yellow", 3, 2, f"Неподтверждённых (жёлтых) сумм: {yellow}; себестоимость предварительная")
    docs = sorted({m for p in c["parts"] for m in p.get("missing", []) if "Счёт-фактура" in m})
    if docs:
        add("docs", 2, 2, "Нет документов: " + ", ".join(docs))

    # сроки
    for r in ids:
        st = ctx["stage"](r)
        if st:
            d, norm, role = st
            if d > norm:
                late = d - norm
                add("sla", 4 if late > ctx.get("escalate", 2) else 3, 4 if late > ctx.get("escalate", 2) else 3,
                    f"Дней на этапе: {d}; норма: {norm}", owner=role)
                break
    first = _first_gtd(c)
    age = _days_since(first)
    if age is not None and age > AGING_DAYS:
        add("aging", 4 if age > AGING_DAYS * 2 else 3, 3, f"Дней от ГТД: {age}; партия ещё не завершена",
            owner=ctx["resp"](rid) or "operator")
    # склад без выпуска
    if c["keys"]["m"][0] in ("only74", "only70", "only7074") and age is not None and age > WAREHOUSE_DAYS:
        add("warehouse", 4, 4, f"Дней под таможенным складом или временным хранением без ИМ40: {age}; "
                               f"порог: {WAREHOUSE_DAYS}")
    return out


def portfolio_signals(cargos: list[dict], ctx: dict) -> list[Signal]:
    """Риски всего импорта: зависимость от поставщика и страны, рост расходов, работа бота."""
    out: list[Signal] = []
    year = [c for c in cargos if _days_since(c.get("date")) is not None and _days_since(c["date"]) <= 365]
    n = len(year)
    if n >= 10:
        for dim, kind, lim in (("s", "supplier_share", SUPPLIER_SHARE), ("c", "country_share", COUNTRY_SHARE)):
            cnt: dict[str, list] = {}
            for c in year:
                key, label = c["keys"][dim]
                cnt.setdefault(key, [label, 0])[1] += 1
            key, (label, k) = max(cnt.items(), key=lambda x: x[1][1])
            share = k / n * 100
            if share > lim:
                out.append(Signal(kind, 3, 4 if share > lim + 15 else 3,
                                  f"{label}: {share:.0f}%; партий за 12 месяцев: {k} из {n}; порог: {lim:.0f}%",
                                  f"{dim}:{key}", label, KINDS[kind][3]))
        # рост расходов у поставщика: медиана последних 3 месяцев выше медианы года на 20%+
        by_sup: dict[str, list] = {}
        for c in year:
            pct = _num(c["agg"].get("calc_overhead_pct"))
            if pct is not None and 0 < pct < 300:
                by_sup.setdefault(c["keys"]["s"][0], [c["keys"]["s"][1], [], []])
                entry = by_sup[c["keys"]["s"][0]]
                entry[1].append(pct)
                if _days_since(c["date"]) <= 92:
                    entry[2].append(pct)
        for key, (label, all_, recent) in by_sup.items():
            if len(all_) >= 8 and len(recent) >= 3:
                m_all, m_rec = _median(all_), _median(recent)
                if m_all and m_rec >= m_all * 1.2 and m_rec - m_all >= 5:
                    out.append(Signal("supplier_trend", 3, 3,
                                      f"{label}: расходы к заводу за 3 месяца: {m_rec:.0f}%; за год: {m_all:.0f}%",
                                      f"st:{key}", label, "director"))
    bot = ctx.get("bot", {})
    if bot.get("sheet_down"):
        out.append(Signal("bot_sheet", 5, 4, bot["sheet_down"], "bot:sheet", "Таблица", "developer"))
    if bot.get("backup_days") is not None and bot["backup_days"] > 2:
        out.append(Signal("bot_backup", 3, 4, f"Дней с последней копии базы: {bot['backup_days']}", "bot:backup",
                          "Копия базы", "developer"))
    if bot.get("stale"):
        out.append(Signal("bot_stale", 4, 3, bot["stale"], "bot:stale", "Файл таблицы", "developer"))
    return out
