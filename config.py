"""
Все настройки бота. Значения берутся из .env, структура таблицы описана ниже.
Если в таблице добавятся столбцы — допишите их в COLUMNS.
"""

import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Файл настроек: .env (или env / env.txt — Windows часто прячет файлы с точкой в начале).
# Ищем рядом с bot.py, даже если бот запущен из другой папки.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = ""
for _name in (".env", "env", "env.txt", ".env.txt"):
    _path = os.path.join(BASE_DIR, _name)
    if os.path.exists(_path):
        load_dotenv(_path, encoding="utf-8-sig")
        ENV_FILE = _path
        break


def clean_token(raw: str) -> str:
    """Токен без лишнего: пробелы, кавычки, невидимые символы, «bot» в начале, лишний текст после копирования."""
    import re
    t = (raw or "").strip().strip('"').strip("'").replace("\ufeff", "").replace("\u200b", "")
    t = re.sub(r"\s+", "", t)
    if t.lower().startswith("bot") and ":" in t:
        t = t[3:]
    m = re.search(r"\d{6,12}:[A-Za-z0-9_-]{30,}", t)   # из «Use this token…: 123:ABC…» берём сам токен
    return m.group(0) if m else t


def token_ok(t: str) -> bool:
    import re
    return bool(re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", t or ""))


def _ids(name: str) -> set[int]:
    return {int(x) for x in os.getenv(name, "").replace(" ", "").split(",") if x}


# --- Доступы -----------------------------------------------------------------
TELEGRAM_TOKEN = clean_token(os.getenv("TELEGRAM_BOT_TOKEN", ""))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID", "")
WORKSHEET_GID = os.getenv("GOOGLE_WORKSHEET_GID", "").strip()
WORKSHEET_NAME = os.getenv("GOOGLE_WORKSHEET_NAME", "Лист1")
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
# Сколько строк сверху занимает шапка. Бот сам находит строку с «Отправитель» в столбце B;
# это число используется, только если такой строки нет в первых 5 строках.
HEADER_ROWS = int(os.getenv("HEADER_ROWS", "2"))

# Тестовый режим: читать скачанный Excel-файл вместо Google Таблицы.
# Укажите имя файла (например prihod.xlsx) — тогда доступ к Google не нужен.
EXCEL_FILE = os.getenv("EXCEL_FILE", "").strip()
EXCEL_SHEET_NAME = os.getenv("EXCEL_SHEET_NAME", "").strip()  # пусто — первый лист файла

# Кто может пользоваться ботом. Пусто — бот никому не показывает данные (только /id).
# Роли выбираются кнопками в самом боте — раздел «👥 Команда» (Генеральный Директор, Разработчик,
# 1С УНФ-Оператор, Специалист по таможенному оформлению, Сотрудник, Наблюдатель).
# В этом файле — «запасной вход» и начальные списки:
#   OWNER_IDS         — администраторы: полный доступ, уведомления о сменах статуса, управление командой.
#                       Заданы только здесь и из бота не отбираются (страховка от потери доступа);
#                       должность (Директор / Разработчик) им можно выбрать в боте.
#   ALLOWED_USER_IDS  — 👷 Сотрудники: просмотр + смена статусов.
#   VIEWER_USER_IDS   — 👁 Наблюдатели: только просмотр.
# Роль, выбранная кнопками в боте, главнее строк ALLOWED_USER_IDS / VIEWER_USER_IDS.
ALLOWED_USERS = _ids("ALLOWED_USER_IDS")
VIEWER_IDS = _ids("VIEWER_USER_IDS")
OWNER_IDS = _ids("OWNER_IDS")

# --- Claude ------------------------------------------------------------------
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
CLAUDE_MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "3000"))

# --- Работа бота -------------------------------------------------------------
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent"))
SYNC_INTERVAL_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))  # как часто проверять таблицу
DAILY_REMINDER_TIME = os.getenv("DAILY_REMINDER_TIME", "09:00")         # ежедневная сводка
OVERDUE_DAYS = int(os.getenv("OVERDUE_DAYS", "7"))                       # «просрочено», если дольше

# Сроки этапов: сколько дней груз может быть у ответственного, потом — напоминание, затем — Директору
SLA_CUSTOMS_DAYS = int(os.getenv("SLA_CUSTOMS_DAYS", "3"))      # у Специалиста по таможенному оформлению
SLA_UNF_DAYS = int(os.getenv("SLA_UNF_DAYS", "2"))              # у 1С УНФ-Оператора
SLA_ESCALATE_DAYS = int(os.getenv("SLA_ESCALATE_DAYS", "2"))    # ещё столько дней после нормы — сообщение Директору
SLA_REMIND_TIME = os.getenv("SLA_REMIND_TIME", "10:00")         # когда в день рассылать напоминания о сроках
# Недельный отчёт Директору: день недели (0 — понедельник … 4 — пятница) и время
WEEKLY_REPORT_DAY = int(os.getenv("WEEKLY_REPORT_DAY", "4"))
WEEKLY_REPORT_TIME = os.getenv("WEEKLY_REPORT_TIME", "17:00")
# Курс ЦБ (cbu.uz): допустимое отличие курса ГТД (H) от курса ЦБ на дату ГТД и рыночного (S)
CBU_ENABLED = os.getenv("CBU_ENABLED", "true").lower() in ("1", "true", "yes", "да")
CBU_H_TOLERANCE = float(os.getenv("CBU_H_TOLERANCE", "0.01"))   # 1%
CBU_S_TOLERANCE = float(os.getenv("CBU_S_TOLERANCE", "0.15"))   # 15% — рыночный курс может отличаться

# Управление рисками (пороги можно менять в env)
RISK_COST_PCT = float(os.getenv("RISK_COST_PCT", "30"))              # расходы выше обычных у поставщика на 30%+
RISK_SUPPLIER_SHARE = float(os.getenv("RISK_SUPPLIER_SHARE", "50"))  # один поставщик > 50% партий за год
RISK_COUNTRY_SHARE = float(os.getenv("RISK_COUNTRY_SHARE", "70"))    # одна страна > 70% партий за год
RISK_WAREHOUSE_DAYS = int(os.getenv("RISK_WAREHOUSE_DAYS", "30"))    # ИМ74/ИМ70 без ИМ40 дольше 30 дней
RISK_AGING_DAYS = int(os.getenv("RISK_AGING_DAYS", "30"))            # открыта дольше 30 дней от ГТД
RISK_ALERT_SCORE = int(os.getenv("RISK_ALERT_SCORE", "15"))          # с какого балла оповещать сразу (🔴)
RISK_REGISTER_SCORE = int(os.getenv("RISK_REGISTER_SCORE", "8"))     # с какого балла риск попадает в реестр (🟠)
MERGE_ADJACENT_ROWS = os.getenv("MERGE_ADJACENT_ROWS", "true").lower() in ("1", "true", "yes", "да")
# При самом первом запуске все грузы из таблицы уходят в архив (без напоминаний).
# Укажите номер строки, начиная с которой грузы сразу считаются открытыми работами (0 — ни одной).
OPEN_FROM_ROW = int(os.getenv("OPEN_FROM_ROW", "0"))
# Открытые работы начинаются с этого груза (точное название из столбца A); все грузы выше — закрыты.
# Надёжнее номера строки: в Excel и в Google номера строк могут отличаться на 1.
OPEN_FROM_CARGO = os.getenv("OPEN_FROM_CARGO", "").strip()
# Цвет заливки «сумма не подтверждена» (жёлтый). Несколько цветов — через запятую, формат RRGGBB.
UNCONFIRMED_COLORS = {c.strip().upper().lstrip("#")[-6:]
                      for c in os.getenv("UNCONFIRMED_COLORS", "FFFF00").split(",") if c.strip()}
DB_FILE = os.getenv("DB_FILE", "bot.db")

# --- Надёжность и прозрачность (все значения необязательны) --------------------
def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "да", "on")


# Автокопии базы bot.db: папка, сколько последних хранить, во сколько делать каждый день
BACKUP_DIR = os.getenv("BACKUP_DIR", "backups")
# Документы партий: копии присланных файлов хранятся в этой папке (по партиям). false — только в Telegram
DOCS_DIR = os.getenv("DOCS_DIR", "docs")
DOCS_SAVE = os.getenv("DOCS_SAVE", "true").strip().lower() in ("1", "true", "yes", "да", "on")
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "14"))
BACKUP_TIME = os.getenv("BACKUP_TIME", "03:00")
# По понедельникам присылать свежую копию файлом администраторам в Telegram
BACKUP_TO_ADMINS = _flag("BACKUP_TO_ADMINS", "true")
# Через сколько минут бесперебойных ошибок чтения таблицы сообщить администраторам
SHEET_ALERT_MINUTES = int(os.getenv("SHEET_ALERT_MINUTES", "15"))
# Excel-режим: если файл не менялся дольше стольких дней — предупредить в сводке (0 — не проверять)
EXCEL_STALE_DAYS = int(os.getenv("EXCEL_STALE_DAYS", "3"))
# Присылать администраторам сообщение «бот запущен»
STARTUP_NOTICE = _flag("STARTUP_NOTICE", "true")
# Необязательно: адрес внешнего «пульса» (healthchecks.io и т. п.) — бот обращается к нему раз в
# SYNC_INTERVAL_SECONDS, пока таблица читается. Если бот остановился, сервис сам пришлёт сигнал.
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL", "").strip()

# --- Контроль закрытия (все значения необязательны) -----------------------------
# Перед закрытием груза с жёлтыми ячейками / пустыми расходами / без цены завода — спросить подтверждение,
# а закрытие отметить «⚠️ с пометкой» (видно Директору, в журнале и в сводке)
CONFIRM_RISKY_CLOSE = _flag("CONFIRM_RISKY_CLOSE", "true")
# Завершённый груз снова открывается, если в нём появилась новая жёлтая ячейка (сумма не подтверждена)
AUTO_REOPEN_YELLOW = _flag("AUTO_REOPEN_YELLOW", "true")

DETAIL_LIMIT = int(os.getenv("DETAIL_LIMIT", "3"))
LIST_LIMIT = int(os.getenv("LIST_LIMIT", "25"))
LOGISTICS_UZS_THRESHOLD = float(os.getenv("LOGISTICS_UZS_THRESHOLD", "1000000"))
# В старых строках досмотр (BB) и инспектор (BC) вписаны в сумах: значение больше порога считается в сумах
USD_FIELD_UZS_THRESHOLD = float(os.getenv("USD_FIELD_UZS_THRESHOLD", "10000"))

# --- Структура таблицы: буква -> (ключ, подпись, тип) ----------------------
#   text — текст как в таблице;  uzs / usd — суммы;  rate — курс;  num — прочие числа
COLUMNS: dict[str, tuple[str, str, str]] = {
    "A":  ("cargo",              "Наименование груза",                          "text"),
    "B":  ("sender",             "Отправитель",                                  "text"),
    "C":  ("receiver",           "Получатель",                                   "text"),
    "D":  ("gtd_1",              "ГТД, часть 1",                                 "text"),
    "E":  ("gtd_2",              "ГТД, часть 2 (дата)",                          "text"),
    "F":  ("gtd_3",              "ГТД, часть 3",                                 "text"),
    "G":  ("customs_value",      "Таможенная стоимость",                         "num"),
    "H":  ("usd_rate_gtd",       "Курс USD/UZS на дату ГТД",                     "rate"),
    "I":  ("country_dispatch",   "Страна отправления (код)",                     "text"),
    "J":  ("country_origin",     "Страна происхождения (код)",                   "text"),
    "K":  ("incoterms",          "Условия поставки",                             "text"),
    "L":  ("vehicle",            "Номер автотранспорта",                         "text"),
    "M":  ("invoice_gtd",        "Фактурная стоимость в ГТД",                    "num"),
    "N":  ("factory_usd",        "Фактурная стоимость (завод), USD",             "usd"),
    "O":  ("factory_contract",   "Фактурная стоимость (завод), валюта контракта", "num"),
    "P":  ("paid_goods_usd",     "Расход по переводу оплаты (банк), USD",        "usd"),
    "Q":  ("contract_uzs_rate",  "Курс валюты контракта к UZS",                  "rate"),
    "R":  ("contract_usd_rate",  "Курс валюты контракта к USD",                  "rate"),
    "S":  ("market_usd_rate",    "Рыночный курс USD/UZS",                        "rate"),
    "T":  ("places",             "Количество мест",                              "num"),
    "U":  ("net_kg",             "Вес нетто, кг",                                "num"),
    "V":  ("gross_kg",           "Вес брутто, кг",                               "num"),
    "W":  ("volume_m3",          "Объём, м³",                                    "num"),
    "X":  ("kkdg",               "Номер ККДГ",                                   "text"),
    "Y":  ("fee_10",             "Таможенный сбор (код 10), UZS",                "uzs"),
    "Z":  ("duty_20",            "Пошлина (код 20), UZS",                        "uzs"),
    "AA": ("excise_27",          "Акциз (код 27), UZS",                          "uzs"),
    "AB": ("vat_29",             "НДС (код 29), UZS",                            "uzs"),
    "AC": ("customs_total",      "Итого таможенные платежи и сборы, UZS",        "uzs"),
    "AD": ("hyg_cash_uzs",       "Гигиенический сертификат: наличные, UZS",      "uzs"),
    "AE": ("hyg_transfer_uzs",   "Гигиенический сертификат: перечислено, UZS",   "uzs"),
    "AF": ("hyg_invoice",        "Гигиенический сертификат: счёт-фактура",       "text"),
    "AG": ("cert_cash_uzs",      "Сертификат соответствия: наличные, UZS",       "uzs"),
    "AH": ("cert_transfer_uzs",  "Сертификат соответствия: перечислено, UZS",    "uzs"),
    "AI": ("cert_invoice",       "Сертификат соответствия: счёт-фактура",        "text"),
    "AJ": ("eco_cash_uzs",       "Экология: наличные, UZS",                      "uzs"),
    "AK": ("eco_transfer_uzs",   "Экология: перечислено, UZS",                   "uzs"),
    "AL": ("eco_invoice",        "Экология: счёт-фактура",                       "text"),
    "AM": ("quar_cash_uzs",      "Карантин (ИКР): наличные, UZS",                "uzs"),
    "AN": ("quar_transfer_uzs",  "Карантин (ИКР): перечислено, UZS",             "uzs"),
    "AO": ("quar_invoice",       "Карантин (ИКР): счёт-фактура",                 "text"),
    "AP": ("expert_uzs",         "Экспертное заключение, UZS",                   "uzs"),
    "AQ": ("expert_invoice",     "Экспертное заключение: счёт-фактура",          "text"),
    "AR": ("ar_uzs",             "Расход AR (без заголовка), UZS",               "uzs"),
    "AS": ("ar_invoice",         "Расход AR: счёт-фактура (AS)",                 "text"),
    "AT": ("at_cash_uzs",        "Расход AT/AU: наличные, UZS",                  "uzs"),
    "AU": ("au_transfer_uzs",    "Расход AT/AU: перечислено, UZS",               "uzs"),
    "AV": ("au_invoice",         "Расход AT/AU: счёт-фактура (AV)",              "text"),
    "AW": ("logistics_cash_usd", "Логистика: наличные расходы, USD",             "usd"),
    "AX": ("logistics_transfer", "Логистика: перечислено (исходное значение AX)", "num"),
    "AY": ("logistics_company",  "Логистическая компания",                       "text"),
    "AZ": ("logistics_invoice",  "Логистика: № и дата счёт-фактуры",             "text"),
    "BA": ("logistics_contact",  "Контактное лицо логистической компании",       "text"),
    "BB": ("inspection_usd",     "Таможенный досмотр, USD",                      "usd"),
    "BC": ("clearance_usd",      "Таможенное оформление (инспектор), USD",       "usd"),
    "BD": ("factory_pct_usd",    "Процент завода / услуги агента, USD",          "usd"),
    "BE": ("loading_uzs",        "Погрузочно-разгрузочные работы (ПРР), UZS",    "uzs"),
    "BF": ("other_uzs",          "Прочие расходы, UZS",                          "uzs"),
    "BG": ("total_all_usd",      "Итого все расходы с товаром (по таблице), USD", "usd"),
}

# Примечания к ячейкам (правый клик -> «Вставить примечание» / Shift+F2)
NOTE_COLUMNS: dict[str, tuple[str, str]] = {
    "A":  ("cargo_note",       "Примечание к грузу"),
    "BD": ("factory_pct_note", "Примечание к BD (агент)"),
    "BF": ("other_note",       "Примечание к прочим расходам"),
}

# Что бот проверяет на заполненность и показывает в «❗ Не заполнено».
# (подпись, поля — достаточно любого заполненного, условие — проверять, только если заполнено это поле)
REQUIRED_FIELDS: list[tuple[str, list[str], str | None]] = [
    ("Заводская стоимость (N)",          ["factory_usd"], None),
    ("Расход по переводу оплаты (P)",    ["paid_goods_usd"], None),
    ("Рыночный курс (S)",                ["market_usd_rate"], None),
    ("Таможенный сбор (Y)",              ["fee_10"], None),
    ("Пошлина (Z)",                      ["duty_20"], None),
    ("НДС (AB)",                         ["vat_29"], None),
    ("Итого таможня (AC)",               ["customs_total"], None),
    ("Счёт-фактура гиг. сертификата (AF)", ["hyg_invoice"], "hyg_transfer_uzs"),
    ("Сертификат соответствия (AG/AH)",  ["cert_cash_uzs", "cert_transfer_uzs"], None),
    ("Счёт-фактура сертификата (AI)",    ["cert_invoice"], "cert_transfer_uzs"),
    ("Логистика (AW/AX)",                ["logistics_cash_usd", "logistics_transfer"], None),
    ("Логистическая компания (AY)",      ["logistics_company"], None),
    ("Счёт-фактура логистики (AZ)",      ["logistics_invoice"], "logistics_transfer"),
    ("Таможенный досмотр (BB)",          ["inspection_usd"], None),
    ("Таможенное оформление (BC)",       ["clearance_usd"], None),
    ("ПРР (BE)",                         ["loading_uzs"], None),
    ("Итог всех расходов (BG)",          ["total_all_usd"], None),
]
