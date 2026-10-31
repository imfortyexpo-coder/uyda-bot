"""
Рабочие инструменты бота для 1С УНФ-Оператора и Директора (без Telegram):
  • курс ЦБ Узбекистана (cbu.uz) на дату ГТД — сверка курса H и S из таблицы;
  • файлы Excel: шаблон для ввода груза в 1С, выгрузка списков, недельный отчёт.
Только чтение: бот никуда не пишет, кроме своих файлов и базы bot.db.
"""

import io
import json
import logging
from datetime import date

log = logging.getLogger("import-bot")

CBU_URL = "https://cbu.uz/ru/arkhiv-kursov-valyut/json/USD/{day}/"
ORANGE = "FF4900"


# ---------------------------------------------------------------------------
# Курс ЦБ
# ---------------------------------------------------------------------------
async def fetch_cbu_rate(day: date, session=None) -> float | None:
    """Официальный курс USD на дату (сум за 1 $). None — ЦБ не ответил или курса нет."""
    import aiohttp
    url = CBU_URL.format(day=day.isoformat())
    own = session is None
    if own:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            data = json.loads(await resp.text())
        if isinstance(data, list) and data:
            return float(str(data[0].get("Rate", "")).replace(",", ".").replace(" ", ""))
    except Exception as e:
        log.info("Курс ЦБ на %s не получен: %s", day, type(e).__name__)
    finally:
        if own:
            await session.close()
    return None


def rate_check(h: float | None, s: float | None, cbu: float | None, tol_h: float, tol_s: float) -> dict:
    """Сравнение курсов строки с курсом ЦБ. Возвращает отклонения (доли) и флаги."""
    out = {"cbu": cbu, "h_dev": None, "s_dev": None, "h_bad": False, "s_bad": False}
    if not cbu:
        return out
    if h:
        out["h_dev"] = (h - cbu) / cbu
        out["h_bad"] = abs(out["h_dev"]) > tol_h
    if s:
        out["s_dev"] = (s - cbu) / cbu
        out["s_bad"] = abs(out["s_dev"]) > tol_s
    return out


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------
def sheet_title(text: str) -> str:
    """Имя листа Excel: без символов : / \\ ? * [ ] и не длиннее 31."""
    import re
    return (re.sub(r"[:/\\?*\[\]]", " ", text).strip() or "Лист")[:31]


def _book():
    from openpyxl import Workbook
    return Workbook()


def _style_header(ws, row: int, ncols: int) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=ORANGE)
        cell.alignment = Alignment(vertical="center", wrap_text=True)


def _widths(ws, widths: list[int]) -> None:
    from openpyxl.utils import get_column_letter
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _bytes(wb) -> bytes:
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def table_sheet(ws, title: str, headers: list[str], rows: list[list], widths: list[int] | None = None,
                money_cols: tuple[int, ...] = ()) -> None:
    """Лист: заголовок, шапка оранжевым, строки, закреплённая шапка, фильтр, формат чисел."""
    from openpyxl.styles import Font
    ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=13)
    for c, h in enumerate(headers, 1):
        ws.cell(row=3, column=c, value=h)
    _style_header(ws, 3, len(headers))
    for r, row in enumerate(rows, 4):
        for c, v in enumerate(row, 1):
            cell = ws.cell(row=r, column=c, value=v)
            if c in money_cols and isinstance(v, (int, float)):
                cell.number_format = "#,##0.00"
    ws.freeze_panes = "A4"
    if rows:
        from openpyxl.utils import get_column_letter
        ws.auto_filter.ref = f"A3:{get_column_letter(len(headers))}{3 + len(rows)}"
    _widths(ws, widths or [18] * len(headers))


def list_xlsx(title: str, headers: list[str], rows: list[list], money_cols: tuple[int, ...] = (),
              widths: list[int] | None = None, extra: list[tuple[str, list[str], list[list]]] | None = None) -> bytes:
    """Книга Excel из одной или нескольких таблиц."""
    wb = _book()
    ws = wb.active
    ws.title = sheet_title(title)
    table_sheet(ws, title, headers, rows, widths, money_cols)
    for name, h, r in extra or []:
        table_sheet(wb.create_sheet(sheet_title(name)), name, h, r, widths, money_cols)
    return _bytes(wb)


def one_c_xlsx(head: list[tuple[str, object]], costs: list[list], cost_headers: list[str], title: str,
               note: str = "") -> bytes:
    """Шаблон для 1С УНФ: лист «Поступление» (реквизиты) и лист «Доп. расходы» (по статьям)."""
    from openpyxl.styles import Font
    wb = _book()
    ws = wb.active
    ws.title = "Поступление"
    ws.cell(row=1, column=1, value=title).font = Font(bold=True, size=13)
    if note:
        ws.cell(row=2, column=1, value=note).font = Font(italic=True, color="7A7069")
    ws.cell(row=4, column=1, value="Реквизит")
    ws.cell(row=4, column=2, value="Значение")
    _style_header(ws, 4, 2)
    for i, (k, v) in enumerate(head, 5):
        ws.cell(row=i, column=1, value=k)
        cell = ws.cell(row=i, column=2, value=v)
        if isinstance(v, float):
            cell.number_format = "#,##0.00"
    _widths(ws, [38, 46])
    table_sheet(wb.create_sheet("Доп. расходы"), "Дополнительные расходы (для документа «Дополнительные расходы»)",
                cost_headers, costs, [28, 16, 10, 16, 18, 14, 30, 24], money_cols=(2, 4, 5))
    return _bytes(wb)
