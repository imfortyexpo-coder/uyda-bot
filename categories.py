"""
Категории грузов: разные «срезы» одной таблицы — по стране отправления, поставщику, виду транспорта,
получателю, году, условиям поставки и таможенному посту. Всё берётся из самих столбцов таблицы,
ничего не придумывается. Здесь только логика без Telegram — её легко проверить отдельно.

Единица категории — ГРУЗ (части одного груза уже объединены по названию, как во всём боте).
"""

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

# ---------------------------------------------------------------------------
# Справочники
# ---------------------------------------------------------------------------
# (флаг, название) по цифровому коду страны (классификатор стран мира, как в ГТД)
COUNTRIES: dict[str, tuple[str, str]] = {
    "643": ("🇷🇺", "Россия"), "156": ("🇨🇳", "Китай"), "792": ("🇹🇷", "Турция"),
    "784": ("🇦🇪", "ОАЭ"), "364": ("🇮🇷", "Иран"), "112": ("🇧🇾", "Беларусь"),
    "428": ("🇱🇻", "Латвия"), "586": ("🇵🇰", "Пакистан"), "344": ("🇭🇰", "Гонконг"),
    "250": ("🇫🇷", "Франция"), "860": ("🇺🇿", "Узбекистан"), "398": ("🇰🇿", "Казахстан"),
    "417": ("🇰🇬", "Кыргызстан"), "804": ("🇺🇦", "Украина"), "616": ("🇵🇱", "Польша"),
    "380": ("🇮🇹", "Италия"), "276": ("🇩🇪", "Германия"), "410": ("🇰🇷", "Южная Корея"),
    "392": ("🇯🇵", "Япония"), "356": ("🇮🇳", "Индия"), "764": ("🇹🇭", "Таиланд"),
    "704": ("🇻🇳", "Вьетнам"), "702": ("🇸🇬", "Сингапур"), "840": ("🇺🇸", "США"),
    "826": ("🇬🇧", "Великобритания"), "724": ("🇪🇸", "Испания"), "203": ("🇨🇿", "Чехия"),
    "040": ("🇦🇹", "Австрия"), "100": ("🇧🇬", "Болгария"), "642": ("🇷🇴", "Румыния"),
    "496": ("🇲🇳", "Монголия"), "762": ("🇹🇯", "Таджикистан"), "795": ("🇹🇲", "Туркменистан"),
    "031": ("🇦🇿", "Азербайджан"), "268": ("🇬🇪", "Грузия"), "051": ("🇦🇲", "Армения"),
    "233": ("🇪🇪", "Эстония"), "440": ("🇱🇹", "Литва"), "246": ("🇫🇮", "Финляндия"),
    "528": ("🇳🇱", "Нидерланды"), "056": ("🇧🇪", "Бельгия"), "620": ("🇵🇹", "Португалия"),
    "300": ("🇬🇷", "Греция"), "376": ("🇮🇱", "Израиль"), "682": ("🇸🇦", "Саудовская Аравия"),
    "050": ("🇧🇩", "Бангладеш"), "360": ("🇮🇩", "Индонезия"), "458": ("🇲🇾", "Малайзия"),
    "608": ("🇵🇭", "Филиппины"), "158": ("🇹🇼", "Тайвань"),
}

INCOTERMS: dict[str, str] = {
    "EXW": "франко-завод", "FCA": "франко-перевозчик", "CPT": "перевозка оплачена до",
    "CIP": "перевозка и страхование оплачены до", "DAP": "поставка в место назначения",
    "DDP": "поставка с оплатой пошлин", "FOB": "франко-борт", "CFR": "стоимость и фрахт",
    "CIF": "стоимость, страхование, фрахт", "FAS": "франко вдоль борта", "DAT": "поставка на терминал",
}
# Кириллические «двойники» латинских букв (в таблице бывает «СРТ» вместо «CPT»)
_CYR2LAT = str.maketrans("АВСЕНКМОРТХ", "ABCEHKMOPTX")

TRANSPORT = {"auto": ("🚛", "Автотранспорт"), "rail": ("🚆", "Железная дорога"),
             "other": ("📦", "Другое"), "none": ("❔", "Не указан")}

# dim -> (иконка, название раздела, что внутри, одно значение)
DIMENSIONS: dict[str, tuple[str, str, str]] = {
    "c": ("🌍", "Страны", "страна отправления"),
    "m": ("📜", "Таможенные режимы", "таможенный режим (ИМ40 / ИМ74 / ИМ70 / НД40 / ИМ51)"),
    "s": ("🏭", "Поставщики", "отправитель"),
    "t": ("🚛", "Транспорт", "вид транспорта"),
    "r": ("🏢", "Получатели", "получатель"),
    "y": ("📅", "Годы", "год по ГТД выпуска (ИМ40)"),
    "i": ("📦", "Условия поставки", "условие поставки (Инкотермс)"),
    "p": ("🛃", "Таможенные посты", "код таможенного поста из номера ГТД"),
}
DIM_ORDER = list(DIMENSIONS)

IM_WAREHOUSE = re.compile(r"(?i)(?<![\w])(им|im)\s*7\d(?![\w])")   # ИМ70/ИМ74 — склад, не выпуск (ИМ40)
_LEGAL = {"ооо", "оао", "ао", "зао", "ип", "чп", "llc", "ltd", "co", "limited", "inc", "corp", "company",
          "sti", "as", "a", "s", "sa", "gmbh"}


# ---------------------------------------------------------------------------
# Нормализация значений
# ---------------------------------------------------------------------------
def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").lower().replace("ё", "е")).strip()


def vkey(raw: str) -> str:
    """Ключ значения для callback_data: короткие простые значения — как есть, остальные — хэш."""
    raw = str(raw)
    if re.fullmatch(r"[A-Za-z0-9_]{1,12}", raw):
        return raw
    return "h" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def company_key(name: str) -> str:
    """Название компании без формы собственности, знаков и регистра: «ООО "ПОСУДА"» -> «посуда»."""
    tokens = [t for t in re.split(r"[^0-9a-zа-я]+", _norm(name)) if t]
    core = [t for t in tokens if t not in _LEGAL]
    return " ".join(core or tokens)


def country_of(code: str) -> tuple[str, str]:
    code = str(code or "").strip()
    if code in ("", "0"):
        return "none", "Не указана"
    if code in COUNTRIES:
        flag, name = COUNTRIES[code]
        return code, f"{flag} {name}"
    if not code.isdigit():
        return vkey(code), code
    return code, f"🏳️ Код {code}"


def transport_of(vehicle: str) -> tuple[str, str]:
    t = _norm(vehicle)
    if not t:
        key = "none"
    elif re.match(r"^(авто|abto|auto)", t):
        key = "auto"
    elif re.match(r"^(жд|ж/д|ж\.д)", t):
        key = "rail"
    else:
        key = "other"
    icon, name = TRANSPORT[key]
    return key, f"{icon} {name}"


def incoterm_of(text: str) -> tuple[str, str]:
    m = re.match(r"\s*([A-Za-zА-Яа-я]{3})(?![A-Za-zА-Яа-я])", str(text or ""))
    code = m.group(1).upper().translate(_CYR2LAT) if m else ""
    if code in INCOTERMS:
        return code, f"{code} · {INCOTERMS[code]}"
    return "none", "Не указано"


def im40_date(parts: list[dict]) -> date | None:
    """Дата груза — по ГТД выпуска (ИМ40), а не по складской (ИМ70/ИМ74). Частей несколько — последняя."""
    dates = [p["gtd_date"] for p in parts if p.get("gtd_date") and not IM_WAREHOUSE.search(p.get("title", ""))]
    dates = dates or [p["gtd_date"] for p in parts if p.get("gtd_date")]
    return max(dates) if dates else None


# ---------------------------------------------------------------------------
# Режимы деклараций (метка в названии груза, столбец A)
#   ИМ40 — Импорт (выпуск для свободного обращения); ИМ74 — Таможенный склад; ИМ70 — Временное хранение;
#   НД40 — Неполная декларация; ИМ51 — Переработка на таможенной территории.
# Груз без метки — обычная декларация ИМ40. ИМ74 / ИМ70 — «хвосты» груза, идущие до выпуска.
# ---------------------------------------------------------------------------
REGIME_MARK = re.compile(r"(?i)(?<![\w])(им|im|нд|nd)\s*(\d{2})(?![\w])")

REGIMES: dict[str, str] = {
    "im40": "ИМ40 · Импорт",
    "im40_74": "ИМ40 + ИМ74 · через таможенный склад",
    "im40_70": "ИМ40 + ИМ70 · через временное хранение",
    "im40_7074": "ИМ40 + ИМ70 + ИМ74",
    "nd40": "НД40 · Неполная декларация",
    "im51": "ИМ51 · Переработка на таможенной территории",
    "only74": "Только ИМ74 · декларации ИМ40 в таблице нет",
    "only70": "Только ИМ70 · декларации ИМ40 в таблице нет",
    "only7074": "Только ИМ70 и ИМ74 · декларации ИМ40 нет",
    "other": "Другой режим",
}
REGIME_ALIASES: dict[str, set[str]] = {
    "im40": {"им40", "им 40", "импорт"}, "im40_74": {"им74", "им 74", "таможенный склад"},
    "im40_70": {"им70", "им 70", "временное хранение"}, "nd40": {"нд40", "нд 40", "nd40", "неполная декларация"},
    "im51": {"им51", "им 51", "переработка"},
}


def regime_codes(title: str) -> set[str]:
    """Режимы, указанные в названии строки: «Коко 1 авто ИМ74» -> {«ИМ74»}; без метки — пусто."""
    out = set()
    for m in REGIME_MARK.finditer(str(title or "")):
        out.add(("ИМ" if m.group(1).lower() in ("им", "im") else "НД") + m.group(2))
    return out


def regime_of(parts: list[dict]) -> tuple[str, str]:
    """Режим груза по всем его частям: (ключ, подпись)."""
    marks = [regime_codes(p.get("title", "")) for p in parts]
    codes = set().union(*marks) if marks else set()
    plain = any(not m for m in marks)
    tails = {c for c in codes if c in ("ИМ70", "ИМ74")}
    if "НД40" in codes:
        key = "nd40"
    elif "ИМ51" in codes:
        key = "im51"
    elif codes - {"ИМ40", "ИМ70", "ИМ74"}:
        key = "other"
    elif plain or "ИМ40" in codes:
        key = {frozenset(): "im40", frozenset({"ИМ74"}): "im40_74", frozenset({"ИМ70"}): "im40_70"}.get(
            frozenset(tails), "im40_7074")
    else:
        key = {frozenset({"ИМ74"}): "only74", frozenset({"ИМ70"}): "only70"}.get(frozenset(tails), "only7074")
    label = REGIMES[key]
    if key == "other":
        label += ": " + ", ".join(sorted(codes - {"ИМ40", "ИМ70", "ИМ74"}))
    return key, label


# ---------------------------------------------------------------------------
# Группы поставщиков (по отправителю, столбец B; запасной вариант — страна)
# ---------------------------------------------------------------------------
GROUPS: dict[str, str] = {
    "posuda": "🇷🇺 ПОСУДА / Алешина · простые «N авто»",
    "osz": "🇷🇺 ОСЗ",
    "bytplast": "🇷🇺 Бытпласт / Пласт",
    "ru_other": "🇷🇺 Прочие российские поставщики",
    "yiwu_agents": "🇨🇳 Посредники Иу (Euro Star, Gelan, Averon, Long Yi, Uakeen)",
    "cn_brand": "🇨🇳 Заводы и бренды (Huaxing, Homezaza, Fissman, Garbo…)",
    "turkey": "🇹🇷 Турция",
    "uae": "🇦🇪 ОАЭ · контейнеры (ARC Middle East…)",
    "belarus": "🇧🇾 Беларусь",
    "france": "🇫🇷 Франция (ARC France)",
    "iran": "🇮🇷 Иран",
    "other": "🏳️ Прочие / страна не указана",
}
GROUP_RULES: list[tuple[str, str]] = [
    ("posuda", r"ПОСУДА|АЛЕШИН"),
    ("osz", r"(?<![A-ZА-Я])ОСЗ(?![A-ZА-Я])"),
    ("bytplast", r"БЫТПЛАСТ|ПЛАСТ ТИМ|(?<![А-Я])ПЛАСТ(?![А-Я])"),
    ("yiwu_agents", r"YIWU EURO|YIWU GELAN|YI WU LONG|AVERON|UAKEEN|YIWU BEAUTY|YIWU BESTWAY"),
    ("cn_brand", r"HUAXING|THINKHOLD|FISSMAN|MAPLE|JIEYANG|AD-NUR|DELI|SHULOPAL|JS HOMEWARE"),
    ("turkey", r"SISECAM|KORKMAZ|SOLMAZER|DEHA|ADA MOTOR|ELCICEK|GUROK|BARANLAR"),
    ("uae", r"ARC MIDDLE|JAZEEL"),
    ("france", r"ARC FRANCE"),
    ("iran", r"ИРАН|IRAN"),
]
COUNTRY_GROUP = {"643": "ru_other", "156": "cn_brand", "792": "turkey", "784": "uae", "112": "belarus",
                 "250": "france", "364": "iran"}


def group_of(sender: str, country_code: str, title: str = "") -> tuple[str, str]:
    """Группа поставщика: по названию отправителя; если не узнали — по слову «Пласт» в названии груза,
    затем по стране отправления."""
    s = str(sender or "").upper().replace("Ё", "Е")
    for key, pattern in GROUP_RULES:
        if re.search(pattern, s):
            return key, GROUPS[key]
    if re.search(r"(?i)пласт", str(title or "")):
        return "bytplast", GROUPS["bytplast"]
    key = COUNTRY_GROUP.get(str(country_code or "").strip(), "other")
    return key, GROUPS[key]


def post_code(raw: str) -> str:
    """Код таможенного поста — всегда 5 цифр: «3002» -> «03002», «3002.0» -> «03002»."""
    v = str(raw or "").strip()
    if re.fullmatch(r"\d+\.0+", v):
        v = v.split(".")[0]
    if v.isdigit() and len(v) < 5:
        v = v.zfill(5)
    return v


def post_of(raw: str) -> tuple[str, str]:
    code = post_code(raw)
    return (vkey(code), f"Пост {code}") if code else ("none", "Не указан")


def _pick(parts: list[dict], *keys: str) -> str:
    """Первое заполненное значение среди частей груза (сначала часть с ценой завода — она «главная»)."""
    ordered = sorted(parts, key=lambda p: not p.get("factory_usd"))
    for k in keys:
        for p in ordered:
            v = str(p.get(k) or "").strip()
            if v:
                return v
    return ""


# ---------------------------------------------------------------------------
# Дополнительные поля записей (нужны для поиска: «Китай», «Автотранспорт», «FCA»)
# ---------------------------------------------------------------------------
def enrich(records: list[dict]) -> None:
    for r in records:
        r["country_name"] = country_of(r.get("country_dispatch") or r.get("country_origin"))[1].split(" ", 1)[-1]
        r["transport_name"] = transport_of(r.get("vehicle"))[1].split(" ", 1)[-1]
        r["incoterm_name"] = incoterm_of(r.get("incoterms"))[0].replace("none", "")


# ---------------------------------------------------------------------------
# Грузы и категории
# ---------------------------------------------------------------------------
def make_cargo(parts: list[dict], aggregate) -> dict:
    """Груз = все части с одним названием. agg — суммы по всем частям (для одной части — она сама)."""
    agg = aggregate(parts) if len(parts) > 1 else parts[0]
    primary = next((p for p in parts if p.get("factory_usd")), parts[0])
    return {"parts": parts, "ids": [p["id"] for p in parts], "agg": agg, "primary": primary,
            "title": parts[0]["title"], "date": im40_date(parts)}


def keys_of(cargo: dict) -> dict[str, tuple[str, str]]:
    """Ключ и подпись груза в каждом разделе категорий: {dim: (ключ, подпись)}."""
    parts = cargo["parts"]
    sender = _pick(parts, "sender")
    receiver = _pick(parts, "receiver")
    d = cargo["date"]
    return {
        "c": country_of(_pick(parts, "country_dispatch", "country_origin")),
        "s": (vkey(company_key(sender)) if sender else "none", sender or "Не указан"),
        "t": transport_of(_pick(parts, "vehicle")),
        "m": regime_of(parts),
        "r": (vkey(company_key(receiver)) if receiver else "none", receiver or "Не указан"),
        "y": (str(d.year), str(d.year)) if d else ("none", "Без даты ГТД"),
        "i": incoterm_of(_pick(parts, "incoterms")),
        "p": post_of(_pick(parts, "gtd_1")),
    }


@dataclass
class Category:
    dim: str
    key: str
    label: str
    cargos: list[dict] = field(default_factory=list)
    variants: Counter = field(default_factory=Counter)   # варианты написания (для компаний)

    @property
    def title(self) -> str:
        if self.dim in ("s", "r") and self.variants:
            return self.variants.most_common(1)[0][0]
        return self.label


def build_cargos(records: list[dict], group_by_base, aggregate) -> list[dict]:
    """Все грузы (части одного названия вместе), новые сверху. У каждого груза — его ключи в категориях."""
    cargos = [make_cargo(g, aggregate) for g in group_by_base([r for r in records if r.get("id")])]
    cargos.sort(key=lambda c: (c["date"] is not None, c["date"] or date.min), reverse=True)
    for c in cargos:
        c["keys"] = keys_of(c)
    return cargos


def index_of(cargos: list[dict]) -> dict[str, dict[str, Category]]:
    """{раздел: {ключ: Category}} по готовому списку грузов."""
    index: dict[str, dict[str, Category]] = {dim: {} for dim in DIM_ORDER}
    for cargo in cargos:
        for dim, (key, label) in cargo["keys"].items():
            cat = index[dim].setdefault(key, Category(dim, key, label))
            cat.cargos.append(cargo)
            if dim in ("s", "r"):
                cat.variants[label] += 1
    return index


def build_index(records: list[dict], group_by_base, aggregate) -> tuple[list[dict], dict[str, dict[str, Category]]]:
    """Возвращает (все грузы, {раздел: {ключ: Category}}). Грузы внутри категории — новые сверху."""
    cargos = build_cargos(records, group_by_base, aggregate)
    return cargos, index_of(cargos)


def find_by_label(index: dict[str, dict[str, Category]], query: str) -> tuple[str, str] | None:
    """Если запрос — это название категории целиком («Китай», «ПОСУДА», «2025», «FCA»), вернёт (раздел, ключ)."""
    q = _norm(query)
    if len(q) < 3:
        return None
    best = None
    for dim, cats in index.items():
        for key, cat in cats.items():
            names = {_norm(cat.title), _norm(re.sub(r"^\S+\s", "", cat.title)), _norm(cat.label), key.lower()}
            if dim in ("s", "r"):
                names.add(company_key(cat.title))
            if dim == "m":
                names |= REGIME_ALIASES.get(key, set())
            if q in names or (dim in ("s", "r") and company_key(query) in names):
                if best is None or len(cat.cargos) > len(index[best[0]][best[1]].cargos):
                    best = (dim, key)
    return best


# ---------------------------------------------------------------------------
# Показатели
# ---------------------------------------------------------------------------
def _n(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def bg_diff(cargo: dict) -> float | None:
    """BG (итог по таблице) минус расчёт бота по ВСЕМУ грузу (все части). None — сходится или нечего сравнивать."""
    agg = cargo["agg"]
    landed = agg.get("calc_landed_usd")
    factory = _n(agg.get("factory_usd"))
    bg = sum(_n(p.get("total_all_usd")) for p in cargo["parts"])
    if landed and bg and bg > factory * 0.5 and abs(bg - landed) > max(5.0, bg * 0.005):
        return bg - landed
    return None


def core_missing(cargo: dict) -> list[str]:
    """Незаполненные расходы без пустых счёт-фактур (они не делают себестоимость «предварительной»)."""
    return [m for m in cargo["agg"].get("missing", []) if "Счёт-фактура" not in m]


def stats(cargos: list[dict]) -> dict:
    """Суммы по списку грузов."""
    out = {"n": len(cargos), "rows": sum(len(c["parts"]) for c in cargos), "factory": 0.0, "overhead": 0.0,
           "landed": 0.0, "landed_ex_vat": 0.0, "net_kg": 0.0, "kg_landed": 0.0, "with_cost": 0,
           "first": None, "last": None}
    for c in cargos:
        a = c["agg"]
        if a.get("calc_landed_usd"):
            out["with_cost"] += 1
            out["factory"] += _n(a.get("factory_usd"))
            out["overhead"] += _n(a.get("calc_overhead_usd"))
            out["landed"] += a["calc_landed_usd"]
            out["landed_ex_vat"] += _n(a.get("calc_landed_ex_vat_usd"))
            if _n(a.get("net_kg")):
                out["net_kg"] += a["net_kg"]
                out["kg_landed"] += a["calc_landed_usd"]
        if c["date"]:
            out["first"] = c["date"] if out["first"] is None else min(out["first"], c["date"])
            out["last"] = c["date"] if out["last"] is None else max(out["last"], c["date"])
    out["overhead_pct"] = out["overhead"] / out["factory"] * 100 if out["factory"] else None
    out["per_kg"] = out["kg_landed"] / out["net_kg"] if out["net_kg"] else None
    return out


def median_pct(cargos: list[dict]) -> float | None:
    """«Обычная» доля расходов к цене завода — медиана по грузам (не искажается ошибками ввода)."""
    vals = sorted(c["agg"]["calc_overhead_pct"] for c in cargos
                  if isinstance(c["agg"].get("calc_overhead_pct"), (int, float)))
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2


def by_year(cargos: list[dict]) -> list[tuple[str, int, float]]:
    """[(год, грузов, себестоимость)] — новые годы сверху."""
    res: dict[str, list] = {}
    for c in cargos:
        y = str(c["date"].year) if c["date"] else "—"
        row = res.setdefault(y, [0, 0.0])
        row[0] += 1
        row[1] += _n(c["agg"].get("calc_landed_usd"))
    return sorted(((y, n, s) for y, (n, s) in res.items()), key=lambda x: x[0], reverse=True)


def bar(share: float, width: int = 10) -> str:
    share = max(0.0, min(1.0, share))
    filled = round(share * width)
    if share > 0 and filled == 0:
        filled = 1
    return "█" * filled + "░" * (width - filled)
