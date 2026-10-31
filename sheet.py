"""
Чтение Google Таблицы и подготовка данных:
разбор строк, примечания, расчёт себестоимости, объединение строк одного груза, поиск.
"""

import hashlib
import logging
import re
from datetime import date, datetime

import gspread
from gspread.utils import ValueRenderOption, absolute_range_name

import config as cfg

# ===========================================================================
# Метаданные полей
# ===========================================================================
CALC_FIELDS: dict[str, tuple[str, str]] = {
    "gtd":                         ("Номер ГТД",                                        "text"),
    "calc_customs_check_diff":     ("⚠ Расхождение: AC − (Y+Z+AA+AB), UZS",             "uzs"),
    "calc_cert_total_uzs":         ("Сертификаты и разрешения итого, UZS",              "uzs"),
    "calc_inspection_uzs":         ("Таможенный досмотр, UZS (в строке указан в сумах)", "uzs"),
    "calc_clearance_uzs":          ("Таможенное оформление, UZS (в строке указано в сумах)", "uzs"),
    "calc_logistics_transfer_uzs": ("Логистика: перечислено, UZS",                      "uzs"),
    "calc_logistics_transfer_usd": ("Логистика: перечислено, USD",                      "usd"),
    "calc_logistics_total_usd":    ("Логистика итого, USD",                             "usd"),
    "calc_other_total_usd":        ("Прочие расходы итого (AR+AT/AU+BB…BF), USD",       "usd"),
    "calc_overhead_usd":           ("Накладные расходы итого, USD",                     "usd"),
    "calc_overhead_uzs":           ("Накладные расходы итого, UZS",                     "uzs"),
    "calc_overhead_pct":           ("Накладные к заводской стоимости, %",               "pct"),
    "calc_landed_usd":             ("Себестоимость с накладными (с НДС), USD",          "usd"),
    "calc_landed_uzs":             ("Себестоимость с накладными (с НДС), UZS",          "uzs"),
    "calc_landed_ex_vat_usd":      ("Себестоимость без НДС, USD",                       "usd"),
    "calc_landed_usd_per_kg":      ("Себестоимость за 1 кг нетто, USD",                 "usd"),
    "calc_total_check_diff":       ("⚠ Расхождение: BG (таблица) − расчёт бота, USD",   "usd"),
    "calc_rate_used":              ("Курс для пересчёта UZS→USD",                       "rate"),
}

META: dict[str, tuple[str, str]] = {k: (lbl, kind) for _, (k, lbl, kind) in cfg.COLUMNS.items()}
META.update({k: (lbl, "text") for _, (k, lbl) in cfg.NOTE_COLUMNS.items()})
META.update(CALC_FIELDS)
SOURCE_KEYS = [k for _, (k, _, _) in cfg.COLUMNS.items()] + [k for _, (k, _) in cfg.NOTE_COLUMNS.items()]

SECTIONS: list[tuple[str, list[str]]] = [
    ("Груз", ["cargo", "cargo_note", "sender", "receiver", "country_dispatch", "country_origin",
              "incoterms", "vehicle", "kkdg", "places", "net_kg", "gross_kg", "volume_m3"]),
    ("ГТД", ["gtd", "customs_value", "invoice_gtd", "usd_rate_gtd"]),
    ("Стоимость товара", ["factory_usd", "factory_contract", "contract_uzs_rate", "contract_usd_rate",
                          "market_usd_rate"]),
    ("Таможенные платежи", ["fee_10", "duty_20", "excise_27", "vat_29", "customs_total",
                            "calc_customs_check_diff"]),
    ("Сертификаты и разрешения", ["hyg_cash_uzs", "hyg_transfer_uzs", "hyg_invoice",
                                  "cert_cash_uzs", "cert_transfer_uzs", "cert_invoice",
                                  "eco_cash_uzs", "eco_transfer_uzs", "eco_invoice",
                                  "quar_cash_uzs", "quar_transfer_uzs", "quar_invoice",
                                  "expert_uzs", "expert_invoice", "calc_cert_total_uzs"]),
    ("Логистика", ["logistics_company", "logistics_contact", "logistics_cash_usd",
                   "calc_logistics_transfer_uzs", "calc_logistics_transfer_usd",
                   "calc_logistics_total_usd", "logistics_invoice"]),
    ("Банковские расходы", ["paid_goods_usd"]),
    ("Прочие расходы", ["inspection_usd", "calc_inspection_uzs", "clearance_usd",
                        "calc_clearance_uzs", "factory_pct_usd", "factory_pct_note",
                        "loading_uzs", "other_uzs", "other_note", "ar_uzs", "ar_invoice",
                        "at_cash_uzs", "au_transfer_uzs", "au_invoice", "calc_other_total_usd"]),
    ("Расчёт себестоимости (вычислено ботом)",
     ["calc_rate_used", "calc_overhead_usd", "calc_overhead_uzs", "calc_overhead_pct",
      "calc_landed_usd", "calc_landed_uzs", "calc_landed_ex_vat_usd", "calc_landed_usd_per_kg",
      "total_all_usd", "calc_total_check_diff"]),
]


# ===========================================================================
# Разбор значений
# ===========================================================================
def col_to_index(letters: str) -> int:
    """'A' -> 0, 'Z' -> 25, 'AA' -> 26."""
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


FIELD_INDEX = {key: col_to_index(letter) for letter, (key, _, _) in cfg.COLUMNS.items()}


def to_num(value) -> float | None:
    """Число из ячейки: понимает 1234.5, «1 234 567,89», «$1,234.50»."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(" ", "").replace(" ", "")
    if not s or re.search(r"[A-Za-zА-Яа-яЁё]", s.replace("сум", "").replace("$", "")):
        return None  # текст (например «№ 123 от 01.02.20») — не число
    s = re.sub(r"[^\d,.\-]", "", s)
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    if s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(text: str) -> date | None:
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", str(text or ""))
    if not m:
        return None
    d, mth, y = (int(x) for x in m.groups())
    if y < 100:
        y += 2000
    try:
        return date(y, mth, d)
    except ValueError:
        return None


def _cell(row: list, idx: int):
    return row[idx] if 0 <= idx < len(row) else ""


def filled(rec: dict, key: str) -> bool:
    return rec.get(key) not in (None, "")


# ===========================================================================
# Расчёты по грузу
# ===========================================================================
def compute(r: dict) -> dict:
    def g(key):
        v = r.get(key)
        return v if isinstance(v, (int, float)) else 0.0

    c: dict = {}
    rate = r.get("market_usd_rate") or r.get("usd_rate_gtd")

    parts = g("fee_10") + g("duty_20") + g("excise_27") + g("vat_29")
    customs_total = r.get("customs_total")
    if customs_total is None:
        customs_total = parts
    elif parts and abs(customs_total - parts) > 1:
        c["calc_customs_check_diff"] = customs_total - parts

    cert_uzs = sum(g(k) for k in ("hyg_cash_uzs", "hyg_transfer_uzs", "cert_cash_uzs", "cert_transfer_uzs",
                                  "eco_cash_uzs", "eco_transfer_uzs", "quar_cash_uzs", "quar_transfer_uzs",
                                  "expert_uzs"))
    if cert_uzs:
        c["calc_cert_total_uzs"] = cert_uzs

    # Досмотр (BB) и инспектор (BC): в старых строках вписаны в сумах
    usd_fixed, uzs_fixed = 0.0, 0.0
    for key, calc_key in (("inspection_usd", "calc_inspection_uzs"), ("clearance_usd", "calc_clearance_uzs")):
        v = g(key)
        if v > cfg.USD_FIELD_UZS_THRESHOLD:
            c[calc_key] = v
            uzs_fixed += v
        else:
            usd_fixed += v

    ax = r.get("logistics_transfer")
    ax_uzs = ax if ax and ax > cfg.LOGISTICS_UZS_THRESHOLD else 0.0
    ax_usd = ax if ax and ax <= cfg.LOGISTICS_UZS_THRESHOLD else 0.0
    if ax_uzs:
        c["calc_logistics_transfer_uzs"] = ax_uzs
    if ax_usd:
        c["calc_logistics_transfer_usd"] = ax_usd

    if not rate:
        return c

    c["calc_rate_used"] = rate
    logistics_usd = g("logistics_cash_usd") + ax_usd + ax_uzs / rate
    if logistics_usd:
        c["calc_logistics_total_usd"] = logistics_usd

    bank_usd = g("paid_goods_usd")  # P — банковский расход за перевод оплаты, отдельная статья
    other_usd = (usd_fixed + g("factory_pct_usd")
                 + (uzs_fixed + g("loading_uzs") + g("other_uzs")
                    + g("ar_uzs") + g("at_cash_uzs") + g("au_transfer_uzs")) / rate)
    if other_usd:
        c["calc_other_total_usd"] = other_usd

    overhead_usd = customs_total / rate + cert_uzs / rate + logistics_usd + bank_usd + other_usd
    c["calc_overhead_usd"] = overhead_usd
    c["calc_overhead_uzs"] = overhead_usd * rate

    factory = r.get("factory_usd")
    if factory:
        landed = factory + overhead_usd
        c["calc_landed_usd"] = landed
        c["calc_landed_uzs"] = landed * rate
        c["calc_landed_ex_vat_usd"] = landed - g("vat_29") / rate
        c["calc_overhead_pct"] = overhead_usd / factory * 100
        if r.get("net_kg"):
            c["calc_landed_usd_per_kg"] = landed / r["net_kg"]
        bg = r.get("total_all_usd")
        if bg and bg > factory * 0.5 and abs(bg - landed) > max(5.0, bg * 0.005):
            c["calc_total_check_diff"] = bg - landed
    return c


def missing_fields(rec: dict) -> list[str]:
    """Подписи незаполненных обязательных полей."""
    result = []
    for label, fields, condition in cfg.REQUIRED_FIELDS:
        if condition and not filled(rec, condition):
            continue
        if not any(filled(rec, f) for f in fields):
            result.append(label)
    return result


# ===========================================================================
# Разбор листа и объединение строк одного груза
# ===========================================================================
def header_rows(formatted: list[list]) -> int:
    """Шапка заканчивается на строке со словом «Отправитель» в столбце B (ищем в первых 5 строках)."""
    b = col_to_index("B")
    for i, row in enumerate(formatted[:5]):
        if "отправител" in str(_cell(row, b)).lower():
            return i + 1
    return cfg.HEADER_ROWS


LETTER_BY_INDEX = {idx: letter for letter, (key, _, _) in cfg.COLUMNS.items() for idx in [col_to_index(letter)]}
KEY_BY_LETTER = {letter: key for letter, (key, _, _) in cfg.COLUMNS.items()}


def parse_rows(formatted: list[list], raw: list[list], notes: dict[str, list[list]],
               yellow: dict[int, set[str]] | None = None) -> list[dict]:
    """
    formatted — значения как видны в таблице, raw — «сырые» числа,
    notes — {буква столбца: примечания этого столбца по строкам},
    yellow — {номер строки (с 0): буквы столбцов с жёлтой заливкой = сумма не подтверждена}.
    """
    yellow = yellow or {}
    rows = []
    for i in range(header_rows(formatted), len(formatted)):
        f_row, r_row = formatted[i], raw[i] if i < len(raw) else []
        rec: dict = {"_row": i + 1}
        for key, idx in FIELD_INDEX.items():
            if META[key][1] == "text":
                rec[key] = str(_cell(f_row, idx)).strip()
            else:
                rec[key] = to_num(_cell(r_row, idx))
        for letter, (key, _) in cfg.NOTE_COLUMNS.items():
            col = notes.get(letter) or []
            n_row = col[i] if i < len(col) else []
            rec[key] = str(_cell(n_row, 0) or "").strip()
        g1 = str(rec["gtd_1"] or "").strip()
        if g1.isdigit() and len(g1) < 5:          # код поста — 5 цифр: «3002» -> «03002»
            rec["gtd_1"] = g1.zfill(5)
        rec["gtd"] = " / ".join(p for p in (rec["gtd_1"], rec["gtd_2"], rec["gtd_3"]) if p)
        if not (rec["cargo"] or rec["gtd"]):
            continue
        # Жёлтые ячейки — только заполненные суммы в столбцах расходов
        rec["unconfirmed"] = sorted(
            {KEY_BY_LETTER[l] for l in yellow.get(i, ()) if l in KEY_BY_LETTER
             and META[KEY_BY_LETTER[l]][1] in ("usd", "uzs", "num") and KEY_BY_LETTER[l] != "total_all_usd"
             and isinstance(rec.get(KEY_BY_LETTER[l]), (int, float)) and rec[KEY_BY_LETTER[l]]})
        rows.append(rec)
    return rows


def normalize(s) -> str:
    return re.sub(r"\s+", " ", str(s).lower().replace("ё", "е")).strip()


def compact(s) -> str:
    return re.sub(r"[\W_]+", "", normalize(s))


def base_name(title: str) -> str:
    """Название груза без вариантов написания: «Коко 1 avto (2-2)», «Коко 1 авто ИМ74» -> «коко 1 авто».
    Убирается номер части (1-4), суффикс режима ИМ74/ИМ70, avto = авто, регистр и лишние пробелы."""
    t = normalize(title)
    t = re.sub(r"(?<![\w])avto(?![\w])", "авто", t)
    t = re.sub(r"\(\s*\d+\s*[-–/]\s*\d+\s*\)", " ", t)
    t = re.sub(r"(?<![\w])(им|im|нд|nd)\s*\d{2}(?![\w])", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -–")


def _merge(group: list[dict]) -> dict:
    """Одна запись из нескольких строк: по каждому полю берётся последнее заполненное значение."""
    rec: dict = {}
    for key in SOURCE_KEYS:
        values = [r[key] for r in group if filled(r, key)]
        if key.endswith("_note"):
            uniq = list(dict.fromkeys(values))
            rec[key] = "\n".join(uniq)
        else:
            rec[key] = values[-1] if values else ("" if META[key][1] == "text" else None)
    gtds = list(dict.fromkeys(r["gtd"] for r in group if r["gtd"]))
    rec["gtd"] = "; ".join(gtds)
    rec["unconfirmed"] = sorted({k for r in group for k in r.get("unconfirmed", [])})
    rec["_rows"] = [r["_row"] for r in group]
    return rec


def group_records(rows: list[dict]) -> list[dict]:
    """Соседние строки с одинаковым названием груза — одна работа (если MERGE_ADJACENT_ROWS)."""
    groups: list[list[dict]] = []
    for r in rows:
        prev = groups[-1][-1] if groups else None
        if (cfg.MERGE_ADJACENT_ROWS and prev and r["cargo"]
                and normalize(prev["cargo"]) == normalize(r["cargo"])
                and r["_row"] == prev["_row"] + 1):
            groups[-1].append(r)
        else:
            groups.append([r])

    records, seen = [], {}
    for group in groups:
        rec = _merge(group)
        rec["title"] = rec["cargo"] or rec["gtd"] or f"Строка {rec['_rows'][0]}"
        rec["base"] = base_name(rec["title"]) or normalize(rec["title"])
        base = normalize(rec["title"])
        seen[base] = seen.get(base, 0) + 1
        rec["key"] = f"{base}#{seen[base]}"
        rec["gtd_date"] = parse_date(rec.get("gtd_2"))
        rec.update(compute(rec))
        rec["missing"] = missing_fields(rec)
        rec["note_hash"] = (hashlib.sha1(rec["cargo_note"].encode()).hexdigest()[:16]
                            if rec.get("cargo_note") else "")
        records.append(rec)
    return records


def rows_label(rec: dict) -> str:
    rows = rec["_rows"]
    return str(rows[0]) if len(rows) == 1 else f"{rows[0]}–{rows[-1]}"


# ===========================================================================
# Чтение из Google
# ===========================================================================
class SheetReader:
    def __init__(self):
        self._gc = gspread.service_account(filename=cfg.SERVICE_ACCOUNT_FILE)

    def fetch(self) -> list[dict]:
        book = self._gc.open_by_key(cfg.SPREADSHEET_ID)
        ws = (book.get_worksheet_by_id(int(cfg.WORKSHEET_GID)) if cfg.WORKSHEET_GID
              else book.worksheet(cfg.WORKSHEET_NAME))
        formatted = ws.get_all_values(combine_merged_cells=True)
        raw = ws.get_all_values(value_render_option=ValueRenderOption.unformatted,
                                combine_merged_cells=True)
        notes = {letter: ws.get_notes(grid_range=f"{letter}:{letter}") for letter in cfg.NOTE_COLUMNS}
        return group_records(parse_rows(formatted, raw, notes, self._yellow(book, ws)))

    @staticmethod
    def _yellow(book, ws) -> dict[int, set[str]]:
        """Ячейки с жёлтой заливкой (сумма не подтверждена) в столбцах расходов."""
        if not cfg.UNCONFIRMED_COLORS:
            return {}
        last = max(cfg.COLUMNS, key=col_to_index)
        try:
            meta = book.fetch_sheet_metadata(params={
                "includeGridData": "true",
                "ranges": absolute_range_name(ws.title, f"A1:{last}"),
                "fields": "sheets.data.rowData.values.effectiveFormat.backgroundColor"})
        except Exception:
            logging.getLogger("import-bot").exception("Не удалось прочитать цвета ячеек — без отметок 🟡")
            return {}
        result: dict[int, set[str]] = {}
        rows = meta["sheets"][0]["data"][0].get("rowData", [])
        for i, row in enumerate(rows):
            for j, cell in enumerate(row.get("values", [])):
                c = cell.get("effectiveFormat", {}).get("backgroundColor")
                if c and google_hex(c) in cfg.UNCONFIRMED_COLORS and j in LETTER_BY_INDEX:
                    result.setdefault(i, set()).add(LETTER_BY_INDEX[j])
        return result


def google_hex(c: dict) -> str:
    return "".join(f"{round(c.get(k, 0) * 255):02X}" for k in ("red", "green", "blue"))


def _excel_text(v) -> str:
    """Значение ячейки Excel в текст так, как его видно в таблице."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.strftime("%d.%m.%Y %H:%M")
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


class ExcelReader:
    """Тестовый режим: читает .xlsx (Google Таблицы -> Файл -> Скачать -> Microsoft Excel).
    Примечания Google в Excel становятся комментариями к ячейкам — бот читает и их."""

    def __init__(self, path: str, sheet_name: str = ""):
        self.path = path
        self.sheet_name = sheet_name
        self._logged = False

    def fetch(self) -> list[dict]:
        from openpyxl import load_workbook  # импорт здесь, чтобы режим Google не требовал openpyxl

        wb = load_workbook(self.path, data_only=True)  # формулы -> сохранённые значения
        if not self._logged:
            print(f"Excel: листы в файле — {', '.join(wb.sheetnames)}")
            self._logged = True
        if self.sheet_name and self.sheet_name not in wb.sheetnames:
            raise ValueError(f"В файле нет листа «{self.sheet_name}». Есть: {', '.join(wb.sheetnames)}")
        ws = wb[self.sheet_name] if self.sheet_name else wb.worksheets[0]

        max_col = max(max(FIELD_INDEX.values()), *(col_to_index(c) for c in cfg.NOTE_COLUMNS)) + 1
        grid = [[c.value for c in row] + [None] * (max_col - len(row))
                for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=max_col)]
        for rng in ws.merged_cells.ranges:  # объединённые ячейки: значение во всех клетках
            v = ws.cell(rng.min_row, rng.min_col).value
            for r in range(rng.min_row, min(rng.max_row, len(grid)) + 1):
                for c in range(rng.min_col, min(rng.max_col, max_col) + 1):
                    grid[r - 1][c - 1] = v

        formatted = [[_excel_text(v) for v in row] for row in grid]
        raw = [[None if isinstance(v, (datetime, date)) else v for v in row] for row in grid]
        notes = {}
        for letter in cfg.NOTE_COLUMNS:
            col = col_to_index(letter) + 1
            notes[letter] = [[ws.cell(r, col).comment.text.strip() if ws.cell(r, col).comment else ""]
                             for r in range(1, len(grid) + 1)]
        yellow: dict[int, set[str]] = {}
        if cfg.UNCONFIRMED_COLORS:
            for letter, idx in ((l, col_to_index(l) + 1) for l in cfg.COLUMNS):
                for r in range(1, len(grid) + 1):
                    fill = ws.cell(r, idx).fill
                    if fill is not None and fill.fill_type not in (None, "none"):
                        fg = fill.fgColor
                        if fg is not None and fg.type == "rgb" and str(fg.rgb)[-6:].upper() in cfg.UNCONFIRMED_COLORS:
                            yellow.setdefault(r - 1, set()).add(letter)
        return group_records(parse_rows(formatted, raw, notes, yellow))


def make_reader():
    return ExcelReader(cfg.EXCEL_FILE, cfg.EXCEL_SHEET_NAME) if cfg.EXCEL_FILE else SheetReader()


# ===========================================================================
# Поиск
# ===========================================================================
SEARCH_FIELDS = ["cargo", "cargo_note", "sender", "receiver", "gtd", "vehicle", "kkdg",
                 "logistics_company", "logistics_contact", "factory_pct_note", "other_note",
                 "cert_invoice", "logistics_invoice", "country_dispatch", "country_origin", "incoterms"]
CODE_FIELDS = ["gtd", "gtd_1", "gtd_2", "gtd_3", "vehicle", "kkdg"]
STOPWORDS = {"какой", "какая", "какие", "каков", "сколько", "по", "для", "это", "все", "всех",
             "покажи", "показать", "найди", "найти", "дай", "груз", "груза", "грузу", "грузы",
             "сумма", "сумму", "был", "была", "было", "где", "что", "как", "или"}


def _stem(w: str) -> str:
    return w[: max(4, len(w) - 2)] if w.isalpha() and len(w) > 5 else w


def find_by_fields(records: list[dict], fields: list[str], query: str) -> list[dict]:
    q = compact(query)
    if not q:
        return []
    return [r for r in records if any(q in compact(r.get(f, "")) for f in fields)]


def search_cargo(records: list[dict], query: str) -> list[dict]:
    """Поиск по названию груза (столбец A) с учётом разных написаний (части (1-4), ИМ74, avto/авто).
    1) то же название; 2) название содержит запрос целиком (по границам слов);
    3) в названии есть все слова запроса. Порядок — как в таблице."""
    q = base_name(query) or normalize(query)
    if not q:
        return []
    exact = [r for r in records if r["base"] == q]
    if exact:
        return exact
    pattern = re.compile(r"(?<![\w])" + re.escape(q) + r"(?![\w])")
    whole = [r for r in records if pattern.search(r["base"])]
    if whole:
        return whole
    words = [_stem(w) for w in re.split(r"[^\w\-/.]+", q) if w and w not in STOPWORDS]
    if not words:
        return []
    return [r for r in records if all(w in r["base"] for w in words)]


def display_name(title: str) -> str:
    """Название для показа без номера части и суффикса ИМ: «Коко 1 авто (2-2)» -> «Коко 1 авто»."""
    t = re.sub(r"\s*\(\s*\d+\s*[-–/]\s*\d+\s*\)", "", title)
    t = re.sub(r"(?i)\s*(?<![\w])(им|im|нд|nd)\s*\d{2}(?![\w])", "", t)
    return re.sub(r"\s+", " ", t).strip(" -–") or title


def group_by_base(records: list[dict]) -> list[list[dict]]:
    """Части одного груза (одинаковое название без вариантов написания) — в одну группу."""
    groups: dict[str, list[dict]] = {}
    for r in records:
        groups.setdefault(r["base"], []).append(r)
    return list(groups.values())


SUM_SKIP = {"usd_rate_gtd", "contract_uzs_rate", "contract_usd_rate", "market_usd_rate", "calc_rate_used",
            "calc_overhead_pct", "calc_landed_usd_per_kg", "calc_customs_check_diff", "calc_total_check_diff"}


def aggregate(parts: list[dict]) -> dict:
    """Сумма по всем частям груза: каждая статья расходов, цена завода, себестоимость."""
    agg: dict = {"title": parts[0]["title"], "base": parts[0]["base"], "parts": parts}
    keys = {k for p in parts for k, v in p.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in SUM_SKIP and not k.startswith("_")
            and (k in META) and META[k][1] in ("usd", "uzs", "num")}
    for k in keys:
        if k == "inspection_usd":      # в старых строках досмотр в сумах — его уже посчитали отдельно
            vals = [p[k] for p in parts if isinstance(p.get(k), (int, float)) and "calc_inspection_uzs" not in p]
        elif k == "clearance_usd":
            vals = [p[k] for p in parts if isinstance(p.get(k), (int, float)) and "calc_clearance_uzs" not in p]
        else:
            vals = [p[k] for p in parts if isinstance(p.get(k), (int, float))]
        if vals:
            agg[k] = sum(vals)
    # Вес, места, объём часто повторяются в каждой части одной машины — тогда берём один раз
    for k in ("net_kg", "gross_kg", "places", "volume_m3"):
        vals = [p[k] for p in parts if isinstance(p.get(k), (int, float)) and p[k]]
        if vals and len(set(vals)) == 1 and len(vals) > 1:
            agg[k] = vals[0]
    if agg.get("factory_usd") and agg.get("calc_overhead_usd") is not None:
        # Себестоимость всего груза = цена завода + расходы ВСЕХ частей (в т.ч. частей без цены завода, ИМ74)
        vat_usd = sum((p.get("vat_29") or 0) / p["calc_rate_used"] for p in parts if p.get("calc_rate_used"))
        agg["calc_landed_usd"] = agg["factory_usd"] + agg["calc_overhead_usd"]
        agg["calc_landed_ex_vat_usd"] = agg["calc_landed_usd"] - vat_usd
        agg["calc_overhead_pct"] = agg["calc_overhead_usd"] / agg["factory_usd"] * 100
    if agg.get("calc_landed_usd") and agg.get("net_kg"):
        agg["calc_landed_usd_per_kg"] = agg["calc_landed_usd"] / agg["net_kg"]
    rates = {p.get("calc_rate_used") for p in parts if p.get("calc_rate_used")}
    if len(rates) == 1:
        agg["calc_rate_used"] = rates.pop()
    agg["missing"] = sorted({m for p in parts for m in p["missing"]})
    agg["unconfirmed"] = sorted({k for p in parts for k in p.get("unconfirmed", [])})
    return agg


def search_free(records: list[dict], query: str) -> list[dict]:
    words = [_stem(w) for w in re.split(r"[^\w\-/.]+", normalize(query))
             if w and (len(w) >= 3 or w.isdigit()) and w not in STOPWORDS]
    qc = compact(query)
    scored = []
    for r in records:
        text = normalize(" | ".join(str(r.get(f, "")) for f in SEARCH_FIELDS))
        codes = " ".join(compact(r.get(f, "")) for f in CODE_FIELDS)
        score = sum(1 for w in words if w in text)
        if len(qc) >= 4 and qc in codes:
            score += 10
        if score:
            scored.append((score, r))
    if not scored:
        return []
    best = max(s for s, _ in scored)
    scored = [(s, r) for s, r in scored if s >= max(1, best * 0.6)]
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored]


# ===========================================================================
# Форматирование чисел
# ===========================================================================
def fmt_num(x: float, decimals: int) -> str:
    return f"{x:,.{decimals}f}".replace(",", " ").replace(".", ",")


def fmt_value(key: str, v) -> str:
    kind = META.get(key, ("", "text"))[1]
    if kind == "text":
        return str(v)
    if kind == "rate":
        return fmt_num(v, 4).rstrip("0").rstrip(",")
    if kind == "pct":
        return fmt_num(v, 1) + " %"
    if kind == "num":
        if float(v).is_integer():
            return fmt_num(v, 0)
        return fmt_num(v, 3).rstrip("0").rstrip(",")
    return fmt_num(v, 2)


def days_since(d: date | datetime | None, today: date | None = None) -> int | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        d = d.date()
    today = today or datetime.now(cfg.TZ).date()
    return (today - d).days
