"""
Telegram-бот «UYDA Импорт» (GOLD DISHES): учёт импортных грузов и расходов.
Источник — Google Таблица (или Excel-файл в тестовом режиме), статусы — в базе бота.

Что делает:
  • каждые 5 минут читает таблицу; новый груз в столбце A или новое примечание к ячейке A ->
    карточка всем пользователям с кнопками статусов и «Напомнить позже»;
  • каждый день в 09:00 — сводка открытых работ;
  • уведомление о каждой смене статуса — Директору, Разработчику и 1С УНФ-Оператору;
  • роли (Генеральный Директор, Разработчик, 1С УНФ-Оператор, Специалист по таможенному оформлению,
    Сотрудник, Наблюдатель) назначает только Генеральный Директор — кнопками в «👥 Команда» или /adduser;
  • у каждого открытого груза автоматически есть ответственный: расходы не заполнены — Специалист по
    таможенному оформлению, всё заполнено, но груз не завершён — 1С УНФ-Оператор;
  • Генеральный Директор оставляет замечания по грузам («📌 Замечание»), исполнитель отвечает «👍 Принято»;
  • окно «🆘 Поддержка»: 1С УНФ-Оператор пишет вопрос — Разработчик и Директор видят его сразу и отвечают
    прямо в боте; Директор наблюдает за перепиской и может отправить замечание;
  • «📎 Документы»: обмен файлами по партии — Специалист по таможне присылает документы 1С УНФ-Оператору,
    Оператор — готовые Специалисту и Директору; у получателя «✅ Принято», копии — в папке docs;
  • меню внизу экрана: открытые работы, просроченные, поиск, итоги, помощь;
  • списки — кнопками, с фильтрами и страницами; карточки — с переходом ◀ ▶ между грузами.

Запуск: python bot.py   (настройки — в env/.env, структура таблицы — в config.py)
"""

import asyncio
import html
import io
import logging
import os
import re
import time
from datetime import datetime, timedelta

import anthropic
import gspread
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, FSInputFile, ForceReply, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
                           ReplyKeyboardRemove)

import brand
import breakdown as bd
import i18n
import ops
import risks as rk
import categories as ct
import checks as ck
import config as cfg
import reliability as rel
import sheet as sh
from storage import (ACTIVE_ROLES, ADMIN, ADMIN_ROLES, ARCHIVE, CUSTOMS, DENIED, DEVELOPER, DIRECTOR, DONE,
                     EDIT_ROLES, FAIL, NEW, NOTIFY_ROLES, OPEN_STATUSES, OPERATOR, PENDING, STAFF, STATUS_LABELS,
                     VIEWER, WORK, Storage)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("import-bot")
esc = html.escape
BOT_VERSION = "2026-10-31 · две роли у одного человека (15)"   # меняйте при выпуске новых файлов — видно в /status
BOT_DESCRIPTION = ("Учёт импортных грузов GOLD DISHES / UYDA: напоминания о новых грузах и открытых работах, "
                   "статусы, цена завода и все расходы, себестоимость, категории (страны, поставщики, транспорт), "
                   "проверки данных таблицы и журнал действий.")
BOT_SHORT_DESCRIPTION = "Импортные грузы GOLD DISHES / UYDA: статусы, расходы, себестоимость, категории."
sh.SEARCH_FIELDS += ["country_name", "transport_name", "incoterm_name"]   # поиск «Китай», «ЖД», «FCA»

MAX_EVENT_MESSAGES = 10   # больше новых записей за раз — одно общее сообщение
PAGE_SIZE = 8             # грузов на одной странице списка
TABLE_WIDTH = 30          # ширина таблицы расходов (символов) — помещается на экране телефона
STATUS_ICON = {NEW: "🆕", WORK: "🔧", DONE: "✅", FAIL: "⚠️", ARCHIVE: "✅"}
STATUS_NAME = {NEW: "Новая", WORK: "В работе", DONE: "Завершён", FAIL: "Не завершён", ARCHIVE: "Завершён"}
STATUS_SHORT = {NEW: "новая", WORK: "в работе", DONE: "завершён", FAIL: "причина", ARCHIVE: "архив"}
CLOSED_STATUSES = (DONE, ARCHIVE)
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь",
          "Ноябрь", "Декабрь"]
IM_WAREHOUSE = re.compile(r"(?i)(?<![\w])(им|im)\s*7\d(?![\w])")  # ИМ70/ИМ74 — склад, не выпуск (ИМ40)

# Роли пользователей
ROLE_ICON = {ADMIN: "👑", DIRECTOR: "👔", DEVELOPER: "💻", OPERATOR: "🧾", CUSTOMS: "🛃",
             STAFF: "👷", VIEWER: "👁", PENDING: "🆕", DENIED: "⛔"}
ROLE_NAME = {ADMIN: "Администратор", DIRECTOR: "Генеральный Директор", DEVELOPER: "Разработчик",
             OPERATOR: "1С УНФ-Оператор", CUSTOMS: "Специалист по таможенному оформлению",
             STAFF: "Сотрудник", VIEWER: "Наблюдатель", PENDING: "Ожидает решения", DENIED: "Доступ закрыт"}
ROLE_SHORT = {CUSTOMS: "Специалист по таможне"}   # короче — для кнопок
ROLE_INFO = {
    ADMIN: "администратор из файла env: полный доступ; пока Директора нет — управляет командой и ролями",
    DIRECTOR: "полный доступ: статусы, уведомления, журнал, технические команды, управление командой и ролями",
    DEVELOPER: "как оператор (статусы, уведомления) + технические команды, журнал, состояние бота; "
               "команду и роли не меняет",
    OPERATOR: "просмотр, смена статусов, уведомления; отвечает за грузы, где всё заполнено; окно «🆘 Поддержка»",
    CUSTOMS: "просмотр и смена статусов; отвечает за грузы с незаполненными расходами",
    STAFF: "просмотр и смена статусов",
    VIEWER: "только просмотр",
}
SELECTABLE = (DIRECTOR, DEVELOPER, OPERATOR, CUSTOMS, STAFF, VIEWER)   # роли, которые выбирает Администратор
ROLE_CODE = {"g": DIRECTOR, "r": DEVELOPER, "o": OPERATOR, "c": CUSTOMS, "s": STAFF, "v": VIEWER, "x": DENIED}
CODE_OF = {role: code for code, role in ROLE_CODE.items()}   # роль -> код в callback_data
MAX_REQUEST_NOTICES = 20   # если ожидающих запросов больше — администраторам уведомления не шлём (защита от спама)


# Ответственный за груз (считается сам по данным таблицы)
RESP_YELLOW_TO_CUSTOMS = True   # True: неподтверждённая (жёлтая) сумма тоже считается «не заполнено» -> таможня
RESP_ICON = {CUSTOMS: "🛃", OPERATOR: "🧾"}
RESP_TITLE = {CUSTOMS: "Специалист по таможенному оформлению", OPERATOR: "1С УНФ-Оператор"}
INPUT_TTL = 900                 # сколько секунд бот ждёт текст после кнопки «Написать» / «Замечание»


def resp_for(rec: dict | None, status: str) -> str:
    """Кто отвечает за груз: '' — никто (груз завершён), CUSTOMS — есть незаполненное, OPERATOR — всё заполнено."""
    if rec is None or status not in OPEN_STATUSES:
        return ""
    gaps = [m for m in rec.get("missing", []) if "Счёт-фактура" not in m]   # как фильтр ❗ в списке
    if gaps or (RESP_YELLOW_TO_CUSTOMS and rec.get("unconfirmed")):
        return CUSTOMS
    return OPERATOR


def role_label(role: str) -> str:
    return f"{ROLE_ICON[role]} {ROLE_NAME[role]}"


def role_button(role: str, mark: bool = False) -> str:
    return ("✓ " if mark else "") + f"{ROLE_ICON[role]} {ROLE_SHORT.get(role, ROLE_NAME[role])}"


# ===========================================================================
# Приложение: таблица + база + синхронизация
# ===========================================================================
class App:
    def __init__(self):
        self.db = Storage(cfg.DB_FILE)
        self.reader = sh.make_reader()
        self.bot: Bot | None = None
        self.records: list[dict] = []
        self.by_id: dict[int, dict] = {}
        self.order: list[int] = []                # id грузов в порядке таблицы
        self.last_sync = 0.0
        self.started = time.time()
        self.watch = rel.SheetWatch(cfg.SHEET_ALERT_MINUTES * 60)   # читается ли таблица
        self.watch_actions: list[tuple[str, float]] = []            # ('alert'|'recovered', секунд) — ждут отправки
        self.lock = asyncio.Lock()
        self.pending_reason: dict[int, tuple[int, str]] = {}  # user_id -> (id груза, ctx)
        self.nav: dict[int, list[str]] = {}         # user_id -> путь по экранам (для «◀ Назад»)
        self.risk_synced = False                    # реестр рисков сверен с таблицей в этом запуске
        self.screens: dict[int, int] = {}           # user_id -> id сообщения-экрана меню (старый удаляется)
        self.doc_cart: dict[int, dict] = {}         # user_id -> присланные файлы / к какой партии и кому
        self.pending_input: dict[int, tuple] = {}   # user_id -> ('rm'|'sp'|'spr'|'spm', ..., время) — ждём текст
        self.resp_map: dict[int, str] = {}          # id груза -> CUSTOMS / OPERATOR / '' (ответственный)
        self.resp_since: dict[int, int] = {}        # id груза -> с какого времени он у ответственного
        self.searches: dict[int, tuple[str, list[int], list[list[int]]]] = {}  # user_id -> (запрос, id, группы)
        self.search_back: dict[int, str] = {}  # user_id -> кнопка «К списку» (например, месяц в «Завершённых»)
        self.sync_no = 0                          # номер чтения таблицы — для кэша категорий и проверок
        self._cache: dict[str, tuple[int, object]] = {}

    def cached(self, name: str, build):
        """Результат build(), пересчитывается только после нового чтения таблицы."""
        hit = self._cache.get(name)
        if hit and hit[0] == self.sync_no:
            return hit[1]
        value = build()
        self._cache[name] = (self.sync_no, value)
        return value

    async def refresh(self, force: bool = False) -> list[dict]:
        events = []
        async with self.lock:
            fresh = self.records and time.monotonic() - self.last_sync < cfg.SYNC_INTERVAL_SECONDS
            if force or not fresh:
                try:
                    records = await asyncio.to_thread(self.reader.fetch)
                    events = self.sync(records)
                except Exception as e:
                    # запоминаем сбой: если таблица не читается дольше порога, scheduler сообщит администраторам
                    act = self.watch.fail(time.time(), f"{type(e).__name__}: {e}"[:300])
                    if act:
                        self.watch_actions.append(act)
                    raise
                act = self.watch.ok(time.time())
                if act:
                    self.watch_actions.append(act)
                ct.enrich(records)
                self.records = records
                self.last_sync = time.monotonic()
                self.sync_no += 1
        if events:
            await notify_events(events)
        return self.records

    def sync(self, records: list[dict]) -> list[tuple[str, int]]:
        """Сверяет таблицу с базой. Возвращает события: ('new'|'note', id записи)."""
        initialized = self.db.get_meta("initialized") == "1"
        existing = self.db.all_by_key()
        events, by_id = [], {}
        start = self.open_from_index(records)
        for pos, rec in enumerate(records):
            row = existing.get(rec["key"])
            if row is None:
                if initialized and start is not None and pos < start:
                    status = ARCHIVE   # строка выше начала открытых работ — относится к закрытому периоду
                    rid = self.db.insert(rec["key"], rec["title"], status, rec["note_hash"])
                    self.db.set_yellow_hash(rid, ",".join(rec.get("unconfirmed", [])))
                    rec["id"] = rid
                    by_id[rid] = rec
                    continue
                if initialized or (cfg.OPEN_FROM_ROW and rec["_rows"][0] >= cfg.OPEN_FROM_ROW):
                    status = NEW
                else:
                    status = ARCHIVE
                rid = self.db.insert(rec["key"], rec["title"], status, rec["note_hash"])
                self.db.set_yellow_hash(rid, ",".join(rec.get("unconfirmed", [])))
                if initialized:
                    events.append(("new", rid))
            else:
                rid = row["id"]
                if row["title"] != rec["title"]:
                    self.db.update_title(rid, rec["title"])
                if rec["note_hash"] != row["note_hash"]:
                    self.db.set_note_hash(rid, rec["note_hash"])
                    if rec["note_hash"] and initialized:
                        if row["status"] not in OPEN_STATUSES:
                            self.db.set_status(rid, NEW, user_name="таблица: новое примечание", commit=False)
                        events.append(("note", rid))
                # Жёлтые ячейки: завершённый груз снова открывается, если появилась новая неподтверждённая сумма
                yh = ",".join(rec.get("unconfirmed", []))
                old_yh = row["yellow_hash"]
                if old_yh is None:
                    self.db.set_yellow_hash(rid, yh)          # первая проверка после обновления бота — запоминаем
                elif yh != old_yh:
                    added = set(filter(None, yh.split(","))) - set(filter(None, old_yh.split(",")))
                    self.db.set_yellow_hash(rid, yh)
                    current = self.db.get(rid)["status"]
                    if added and initialized and cfg.AUTO_REOPEN_YELLOW and current == DONE:
                        self.db.set_status(rid, NEW, user_name="таблица: новая жёлтая ячейка", commit=False)
                        events.append(("yellow", rid))
            rec["id"] = rid
            by_id[rid] = rec
        self.db.commit()
        if not initialized:
            self.db.set_meta("initialized", "1")
        self.apply_open_from(records)
        self.close_untouched_above(records, start)
        if not initialized:
            log.info("Первый запуск: в базу внесено %d грузов, открытых работ: %d",
                     len(records), len(self.db.open_records()))
        self.by_id = by_id
        self.order = [r["id"] for r in records]
        events += self.update_resp(records, initialized)
        return events

    def update_resp(self, records: list[dict], initialized: bool) -> list[tuple[str, int]]:
        """Пересчитывает ответственных. Событие ('resp', id) — груз передан другой стороне
        (было заполнено не всё -> стало всё, или наоборот). Первое вычисление и повторное открытие — без события."""
        stored = self.db.resp_all()
        status = {r["id"]: r["status"] for r in self.db.db.execute("SELECT id, status FROM records")}
        events, resp_map = [], {}
        for rec in records:
            rid = rec.get("id")
            if rid is None:
                continue
            new = resp_for(rec, status.get(rid, ARCHIVE))
            resp_map[rid] = new
            old = stored.get(rid)
            if old != new:
                self.db.set_resp(rid, new)
                if initialized and old and new:
                    events.append(("resp", rid))
        self.db.commit()
        self.resp_map = resp_map
        self.resp_since = self.db.resp_since()
        self.db.commit()
        return events

    @staticmethod
    def open_from_index(records: list[dict]) -> int | None:
        """Позиция груза OPEN_FROM_CARGO в таблице (None — не задан или не найден)."""
        if not cfg.OPEN_FROM_CARGO:
            return None
        target = sh.normalize(cfg.OPEN_FROM_CARGO)
        return next((i for i, r in enumerate(records) if sh.normalize(r["title"]) == target), None)

    def close_untouched_above(self, records: list[dict], start: int | None) -> None:
        """Разовое исправление: «новые» строки выше начала открытых работ, которых никто не трогал,
        переводятся в завершённые. Строки, где сотрудник уже менял статус, не трогаются."""
        if start is None or self.db.get_meta("above_fix_v1") == "1":
            return
        touched = {r["record_id"] for r in self.db.db.execute("SELECT DISTINCT record_id FROM history WHERE user_id IS NOT NULL")}
        moved = 0
        for rec in records[:start]:
            row = self.db.get(rec["id"]) if rec.get("id") else None
            if row and row["status"] == NEW and row["id"] not in touched:
                self.db.set_status(row["id"], ARCHIVE, user_name="закрыто (выше начала открытых работ)", commit=False)
                moved += 1
        self.db.commit()
        self.db.set_meta("above_fix_v1", "1")
        if moved:
            log.info("Строк выше «%s», переведённых в завершённые: %d", cfg.OPEN_FROM_CARGO, moved)

    def apply_open_from(self, records: list[dict]) -> None:
        """OPEN_FROM_CARGO: груз, с которого начинаются открытые работы. Применяется один раз
        для каждого нового значения: грузы с него и ниже — открытые, все выше — закрыты (архив)."""
        name = cfg.OPEN_FROM_CARGO
        if not name or self.db.get_meta("open_from_applied") == name:
            return
        target = sh.normalize(name)
        start = next((i for i, r in enumerate(records) if sh.normalize(r["title"]) == target), None)
        if start is None:
            log.warning("OPEN_FROM_CARGO: груз «%s» не найден в столбце A — проверьте название", name)
            return
        opened = closed = 0
        for i, rec in enumerate(records):
            row = self.db.get(rec["id"])
            if i >= start and row["status"] == ARCHIVE:
                self.db.set_status(rec["id"], NEW, user_name="начало открытых работ", commit=False)
                opened += 1
            elif i < start and row["status"] in OPEN_STATUSES:
                self.db.set_status(rec["id"], ARCHIVE, user_name="закрыто (до начала открытых работ)", commit=False)
                closed += 1
        self.db.commit()
        self.db.set_meta("open_from_applied", name)
        log.info("Открытые работы начинаются с «%s» (строка %s): открыто %d, закрыто %d",
                 name, records[start]["_rows"][0], opened, closed)


app: App  # создаётся в main()


# ===========================================================================
# Форматирование
# ===========================================================================
def fmt_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, cfg.TZ).strftime("%d.%m.%Y %H:%M")


def days_open(row) -> int:
    return int((time.time() - row["opened_at"]) // 86400)


def plural_days(n: int) -> str:
    return "сегодня" if n <= 0 else f"{n} дн."


def usd(v: float) -> str:
    return f"{sh.fmt_num(v, 2)} $"


def money(v: float | None, cur: str = "$") -> str:
    """Сумма в тексте — моноширинным шрифтом (<code>): видна сразу, не путается с номерами, копируется нажатием.
    Валюта всегда подписана: $ или сум."""
    if v is None:
        return "—"
    return f"<code>{sh.fmt_num(v, 2)} {'$' if cur == '$' else 'сум'}</code>"


def uzs(v: float) -> str:
    return f"{sh.fmt_num(v, 0)} сум"


def num(rec: dict, key: str) -> float | None:
    v = rec.get(key)
    return v if isinstance(v, (int, float)) and v else None


def table(rows: list[tuple[str, str] | None]) -> str:
    """Выровненная таблица в моноширинном блоке. None — разделительная линия."""
    rows = [None if r is None else (i18n.tr(r[0]), i18n.tr(r[1])) for r in rows]   # перевод до выравнивания
    width = max([TABLE_WIDTH] + [len(l) + len(v) + 2 for r in rows if r for l, v in [r]])
    lines = []
    for r in rows:
        if r is None:
            lines.append("─" * width)
        else:
            label, value = r
            lines.append(label + " " * (width - len(label) - len(value)) + value)
    return "<pre>" + esc("\n".join(lines)) + "</pre>"


def short_title(title: str, limit: int = 30) -> str:
    title = i18n.tr(title)          # сначала перевод, потом обрезка — иначе обрезанную фразу не перевести
    return title if len(title) <= limit else title[: limit - 1] + "…"


def status_line(row) -> str:
    s = row["status"]
    text = f"{STATUS_ICON[s]} <b>{STATUS_NAME[s]}</b>"
    if s in OPEN_STATUSES:
        d = days_open(row)
        text += f" · {plural_days(d)}" + (" · ⏰ <b>просрочено</b>" if d >= cfg.OVERDUE_DAYS else "")
    elif s == DONE and row_warn(row):
        text += " · ⚠️ <b>с пометкой</b>"
    return text + f" · №{row['id']}"


def row_warn(row) -> str:
    try:
        return row["warn"] or ""
    except (IndexError, KeyError):
        return ""


def short_usd(v: float) -> str:
    """Крупные суммы коротко: 43,4 млн $ · 512 тыс $ · 830 $."""
    a = abs(v)
    if a >= 1e6:
        return f"{sh.fmt_num(v / 1e6, 1)} млн $"
    if a >= 1e4:
        return f"{sh.fmt_num(v / 1e3, 0)} тыс $"
    return f"{sh.fmt_num(v, 0)} $"


def risk_of(rec: dict | None) -> tuple[list[str], str]:
    """Что мешает закрыть груз «чисто»: (строки для экрана, короткая пометка для журнала)."""
    if not rec:
        return [], ""
    lines, tags = [], []
    yellow = rec.get("unconfirmed", [])
    core = [m for m in rec.get("missing", []) if "Счёт-фактура" not in m]
    if yellow:
        names = ", ".join(dict.fromkeys(sh.META[k][0].split(":")[0].split(",")[0] for k in yellow))
        lines.append(f"🟡 Не подтверждено: <b>{len(yellow)}</b> — {esc(names)}")
        tags.append(f"🟡{len(yellow)}")
    if core:
        lines.append(f"❗ Не заполнено: <b>{len(core)}</b> — {esc(', '.join(core[:3]))}" + ("…" if len(core) > 3 else ""))
        tags.append(f"❗{len(core)}")
    if not rec.get("factory_usd"):
        lines.append("🏭 Нет цены завода (столбец N) — себестоимость не посчитана")
        tags.append("без N")
    return lines, " ".join(tags)


def expense_items(rec: dict) -> list[list[tuple[str, str]]]:
    """Статьи расходов по группам: каждая заполненная статья — отдельной строкой.
    Статья с жёлтой ячейкой в таблице (сумма не подтверждена) помечается звёздочкой *."""
    unconfirmed = set(rec.get("unconfirmed", []))

    def total(*keys):
        return sum(num(rec, k) or 0 for k in keys)

    def L(label: str, *keys: str) -> str:
        return label + "*" if unconfirmed.intersection(keys) else label

    groups: list[list[tuple[str, str]]] = []
    bank = num(rec, "paid_goods_usd")
    if bank:
        groups.append([(L("Банк (перевод)", "paid_goods_usd"), usd(bank))])

    customs = [(L(label, k), uzs(num(rec, k))) for k, label in
               (("fee_10", "Сбор (10)"), ("duty_20", "Пошлина (20)"), ("excise_27", "Акциз (27)"), ("vat_29", "НДС (29)"))
               if num(rec, k)]
    if not customs and num(rec, "customs_total"):
        customs = [(L("Таможня", "customs_total"), uzs(rec["customs_total"]))]
    if customs:
        groups.append(customs)

    certs = [(L(label, *keys), uzs(total(*keys))) for label, keys in
             (("Гиг. сертификат", ("hyg_cash_uzs", "hyg_transfer_uzs")),
              ("Серт. соответ.", ("cert_cash_uzs", "cert_transfer_uzs")),
              ("Экология", ("eco_cash_uzs", "eco_transfer_uzs")),
              ("Карантин", ("quar_cash_uzs", "quar_transfer_uzs")),
              ("Экспертиза", ("expert_uzs",))) if total(*keys)]
    if certs:
        groups.append(certs)

    logistics = []
    if num(rec, "logistics_cash_usd"):
        logistics.append((L("Логистика нал", "logistics_cash_usd"), usd(rec["logistics_cash_usd"])))
    if num(rec, "calc_logistics_transfer_uzs"):
        logistics.append((L("Логистика переч", "logistics_transfer"), uzs(rec["calc_logistics_transfer_uzs"])))
    if num(rec, "calc_logistics_transfer_usd"):
        logistics.append((L("Логистика переч", "logistics_transfer"), usd(rec["calc_logistics_transfer_usd"])))
    if logistics:
        groups.append(logistics)

    other = []
    for usd_key, uzs_key, label in (("inspection_usd", "calc_inspection_uzs", "Досмотр"),
                                    ("clearance_usd", "calc_clearance_uzs", "Инспектор")):
        if num(rec, uzs_key):
            other.append((L(label, usd_key), uzs(rec[uzs_key])))
        elif num(rec, usd_key):
            other.append((L(label, usd_key), usd(rec[usd_key])))
    for key, label, kind in (("factory_pct_usd", "Завод % / агент", "usd"), ("loading_uzs", "ПРР", "uzs"),
                             ("other_uzs", "Прочие", "uzs"), ("ar_uzs", "Расход AR", "uzs")):
        if num(rec, key):
            other.append((L(label, key), usd(rec[key]) if kind == "usd" else uzs(rec[key])))
    if total("at_cash_uzs", "au_transfer_uzs"):
        other.append((L("Расход AT/AU", "at_cash_uzs", "au_transfer_uzs"), uzs(total("at_cash_uzs", "au_transfer_uzs"))))
    if other:
        groups.append(other)
    return groups


def money_rows(rec: dict) -> list[tuple[str, str] | None]:
    """Таблица карточки: цена завода, каждая статья расходов, итоги."""
    rows: list[tuple[str, str] | None] = []
    if num(rec, "factory_usd"):
        label = "Цена завода" + ("*" if "factory_usd" in rec.get("unconfirmed", []) else "")
        rows.append((label, usd(rec["factory_usd"])))
    for group in expense_items(rec):
        rows.append(None)
        rows.extend(group)
    if "calc_overhead_usd" in rec:
        rows.append(None)
        rows.append(("Расходы итого", usd(rec["calc_overhead_usd"])))
        rows.append(("  в сумах", uzs(rec["calc_overhead_uzs"])))
    if "calc_landed_usd" in rec:
        rows.append(("Себестоимость", usd(rec["calc_landed_usd"])))
        rows.append(("Без НДС", usd(rec["calc_landed_ex_vat_usd"])))
        rows.append(("Расходы к заводу", sh.fmt_num(rec["calc_overhead_pct"], 1) + " %"))
        if "calc_landed_usd_per_kg" in rec:
            rows.append(("За 1 кг нетто", usd(rec["calc_landed_usd_per_kg"])))
    if num(rec, "total_all_usd"):
        rows.append(("Итог по таблице", usd(rec["total_all_usd"])))
    if "calc_rate_used" in rec:
        rows.append(("Курс", sh.fmt_value("calc_rate_used", rec["calc_rate_used"])))
    return rows


TYPE_SHORT = {"bank": "Банк", "customs": "Таможня", "certs": "Сертификаты", "logistics": "Логистика",
              "inspection": "Досмотр/оформл.", "agent": "Завод % / агент", "loading": "ПРР", "other": "Прочие"}


def pct(part: float, whole: float) -> str:
    return f"{part / whole * 100:.0f}%" if whole else ""


def breakdown_html(records: list[dict], header: str) -> str:
    """💱 Расходы по валютам (как платили), по видам расходов и по способу оплаты."""
    b = bd.breakdown(records)
    out = [header]
    total = b["total_usd"]
    if not total and not b["no_rate_uzs"]:
        return "\n".join(out + ["", "Расходов в таблице пока нет."])

    cur = []
    if b["paid"][bd.USD]:
        cur.append(("Доллары", usd(b["paid"][bd.USD])))
    if b["paid"][bd.UZS]:
        cur.append(("Сумы", uzs(b["paid"][bd.UZS])))
        if b["uzs_in_usd"]:
            cur.append(("  то же в $", usd(b["uzs_in_usd"])))
    if len(b["rates"]) == 1:
        cur.append(("Курс", sh.fmt_num(next(iter(b["rates"])), 2)))
    elif b["rates"]:
        cur.append(("Курс", "свой у каждой строки"))
    out += ["", "💱 <b>По валютам — как платили</b>", table(cur)]

    rows: list[tuple[str, str] | None] = []
    for kind, _ in bd.TYPES:
        t = b["by_type"][kind]
        if not (t[bd.USD] or t[bd.UZS]):
            continue
        label = TYPE_SHORT[kind] + ("*" if kind in b["unconfirmed"] else "")
        rows.append((label, f"{usd(t['usd'])} {pct(t['usd'], total):>4}"))
        if t[bd.UZS]:
            rows.append(("  в сумах", uzs(t[bd.UZS])))
            if t[bd.USD]:
                rows.append(("  в долларах", usd(t[bd.USD])))
    rows += [None, ("Расходы итого", usd(total) + " 100%")]
    if b["factory_usd"]:
        rows += [("Цена завода", usd(b["factory_usd"])),
                 ("Себестоимость", usd(b["factory_usd"] + total)),
                 ("Расходы к заводу", sh.fmt_num(total / b["factory_usd"] * 100, 1) + " %")]
    out += ["", "🧾 <b>По видам расходов</b> <i>(в $, доля от расходов)</i>", table(rows)]

    pay = [(label, f"{usd(b['by_pay'][k])} {pct(b['by_pay'][k], total):>4}")
           for k, label in ((bd.CASH, "Наличные"), (bd.TRANSFER, "Перечисление"), (bd.UNKNOWN, "Не указано"))
           if b["by_pay"][k]]
    out += ["", "💳 <b>Наличные / перечисление</b>", table(pay),
            "<i>«Не указано» — таможня, досмотр, ПРР, прочие: в таблице нет разделения на нал и переч.</i>"]

    if b["unconfirmed"]:
        out.append("🟡 * — в этом виде есть неподтверждённые суммы (жёлтые ячейки).")
    if b["no_rate_uzs"]:
        out.append(f"ℹ️ Без курса (S/H), в $ не пересчитано: {uzs(b['no_rate_uzs'])} "
                   f"({b['no_rate_rows']} стр.) — в суммы $ выше не вошло.")
    text = "\n".join(out)
    return text if len(text) <= 4000 else text[:3900].rsplit("\n", 1)[0] + "\n…"


def unconfirmed_html(rec: dict) -> str:
    """Блок «🟡 Не подтверждено»: жёлтые ячейки таблицы с суммами."""
    keys = rec.get("unconfirmed", [])
    if not keys:
        return ""
    items = []
    letters = {key: letter for letter, (key, _, _) in cfg.COLUMNS.items()}
    for k in keys:
        v = rec.get(k)
        kind = sh.META[k][1]
        value = usd(v) if kind == "usd" else uzs(v) if kind == "uzs" else sh.fmt_value(k, v)
        items.append(f"• {esc(sh.META[k][0])} ({letters.get(k, '?')}): <b>{value}</b>")
    return (f"🟡 <b>Не подтверждено: {len(keys)}</b> <i>(жёлтые ячейки, в таблице помечены *)</i>\n"
            + "<blockquote>" + "\n".join(items) + "</blockquote>")


def resp_names(role: str) -> str:
    names = [r["name"] or f"ID {r['user_id']}" for r in app.db.list_users() if r["role"] == role]
    if not names:
        return "не назначен"
    return ", ".join(esc(n) for n in names[:3]) + (f" и ещё {len(names) - 3}" if len(names) > 3 else "")


def sla_norm(role: str) -> int:
    return cfg.SLA_CUSTOMS_DAYS if role == CUSTOMS else cfg.SLA_UNF_DAYS


def stage_days(rid: int) -> int | None:
    """Сколько полных дней груз у текущего ответственного."""
    since = app.resp_since.get(rid)
    if not since or not app.resp_map.get(rid):
        return None
    return max(0, int((time.time() - since) // 86400))


def stage_overdue(rid: int) -> bool:
    d = stage_days(rid)
    return d is not None and d > sla_norm(app.resp_map.get(rid, ""))


def resp_line(rid: int) -> str:
    role = app.resp_map.get(rid, "")
    if not role:
        return ""
    why = ("есть незаполненное" if role == CUSTOMS else "всё заполнено, нужно завершить груз")
    d, norm = stage_days(rid), sla_norm(role)
    stage = ""
    if d is not None:
        stage = f"\n      ⏳ На этапе: {plural_days(d)} · норма {norm} дн." + (" · <b>срок прошёл</b>" if d > norm else "")
    return (f"👤 Ответственный: {RESP_ICON[role]} <b>{RESP_TITLE[role]}</b> — {why}\n      <i>{resp_names(role)}</i>"
            + stage)


def remarks_html(rid: int) -> str:
    rows = app.db.open_remarks(rid)
    if not rows:
        return ""
    out = [f"📌 <b>Замечания Генерального Директора: {len(rows)}</b>"]
    for r in rows[-3:]:
        out.append(f"<blockquote>{esc(r['text'])}\n<i>{esc(r['author_name'])}, {fmt_dt(r['created_at'])}</i></blockquote>")
    return "\n".join(out)


def card_html(rid: int, header: str = "") -> str:
    """Карточка груза — кратко: сверху статус и ответственный, затем деньги одной строкой,
    что не заполнено, и данные груза. Полная таблица расходов — кнопкой «💵 Расходы»."""
    row = app.db.get(rid)
    rec = app.by_id.get(rid)
    out = [header, ""] if header else []
    out.append(f"📦 <b>{esc(row['title'])}</b>")
    out.append(status_line(row))
    if resp_line(rid):
        out.append(resp_line(rid))
    if row["status"] in OPEN_STATUSES and risk_line(rid):
        out.append(risk_line(rid))
    if row["status"] == FAIL and row["reason"]:
        out.append(f"<blockquote>Причина: {esc(row['reason'])}</blockquote>")
    if remarks_html(rid):
        out.append("")
        out.append(remarks_html(rid))
    total, waiting = app.db.doc_counts(doc_cargo(rid)[1])
    if total:
        out.append(f"📎 Документы: <b>{total}</b>" + (f" · ждут «Принято»: <b>{waiting}</b>" if waiting else " · все приняты ✅"))
    if rec is None:
        out.append("\nℹ️ Этой записи больше нет в таблице.")
        return "\n".join(out)

    # деньги — одной-двумя строками
    money = []
    if num(rec, "factory_usd"):
        money.append(f"Завод {globals()['money'](rec['factory_usd'])}")
    if "calc_overhead_usd" in rec:
        p = rec.get("calc_overhead_pct")
        word = "расходы" if money else "Расходы"
        money.append(f"{word} {globals()['money'](rec['calc_overhead_usd'])}" + (f" ({sh.fmt_num(p, 0)}%)" if p is not None else ""))
    out.append("")
    if money:
        out.append("💵 " + " · ".join(money))
    if "calc_landed_usd" in rec:
        prelim = any("Счёт-фактура" not in m for m in rec["missing"]) or rec.get("unconfirmed")
        out.append(f"💰 Себестоимость <b>{globals()['money'](rec['calc_landed_usd'])}</b>" + (" · <i>предварительная</i>" if prelim else ""))
    elif not num(rec, "factory_usd"):
        out.append("🏭 Нет цены завода (столбец N) — себестоимость не посчитана")
    else:
        out.append("ℹ️ Нет курса (S/H) — себестоимость не рассчитана")

    # что мешает закрыть
    gaps = rec["missing"]
    if gaps:
        shown = ", ".join(esc(m) for m in gaps[:3]) + ("…" if len(gaps) > 3 else "")
        out.append(f"❗ Не заполнено ({len(gaps)}): {shown}")
    else:
        out.append("✅ Все расходы заполнены")
    if rec.get("unconfirmed"):
        out.append(f"🟡 Не подтверждено: <b>{len(rec['unconfirmed'])}</b> — подробно в «💵 Расходы»")
    if "calc_customs_check_diff" in rec or "calc_total_check_diff" in rec:
        out.append("⚠️ Цифры в таблице не сходятся — подробно в «💵 Расходы»")

    # данные груза — компактно
    out.append("")
    if rec["gtd"]:
        g = f"🧾 ГТД <code>{esc(rec['gtd'])}</code>"
        d = sh.days_since(rec.get("gtd_date"))
        if d is not None and d >= 0:
            g += f" · {plural_days(d)} назад"
        out.append(g)
    cbu = cbu_line(rec)
    if cbu:
        out.append(cbu)
    cats = cargo_keys(rid)
    if cats:
        tags = [cats["m"][1].split(" · ")[0], cats["c"][1], cats["t"][1].split(" ", 1)[-1].lower()]
        if cats["p"][0] != "none":
            tags.append(cats["p"][1].replace("Пост ", "пост "))
        out.append("📜 " + " · ".join(esc(t) for t in tags))
    who = [f"🏭 {esc(rec['sender'])}"] if rec["sender"] else []
    if rec["logistics_company"]:
        who.append(f"🚚 {esc(rec['logistics_company'])}")
    if who:
        out.append(" · ".join(who))
    if rec["vehicle"]:
        out.append(f"🚛 <code>{esc(rec['vehicle'])}</code> · 📄 Строка таблицы: {sh.rows_label(rec)}")
    else:
        out.append(f"📄 Строка таблицы: {sh.rows_label(rec)}")
    if rec["cargo_note"]:
        note = rec["cargo_note"] if len(rec["cargo_note"]) <= 500 else rec["cargo_note"][:500] + "…"
        out.append(f"<blockquote expandable>📝 {esc(note)}</blockquote>")

    if row["status"] == DONE and row_warn(row):
        out.append(f"\n⚠️ <b>Закрыт с пометкой:</b> {esc(row_warn(row))}")
    if row["updated_by"] and row["updated_by"] != "таблица":
        out.append(f"\n<i>Изменено: {esc(row['updated_by'])}, {fmt_dt(row['updated_at'])}</i>")
    return "\n".join(out)


def expenses_html(rid: int) -> str:
    """«💵 Расходы»: цена завода и каждая статья расходов таблицей, жёлтые суммы, расхождения."""
    row = app.db.get(rid)
    rec = app.by_id.get(rid)
    out = [f"📦 <b>{esc(row['title'])}</b>", status_line(row)]
    if rec is None:
        out.append("\nℹ️ Этой записи больше нет в таблице.")
        return "\n".join(out)
    rows = money_rows(rec)
    if rows:
        out.append("\n💵 <b>Цена завода и расходы</b>")
        out.append(table(rows))
        if "calc_landed_usd" in rec and any("Счёт-фактура" not in m for m in rec["missing"]):
            out.append("<i>Себестоимость предварительная — заполнены не все расходы.</i>")
        elif "calc_landed_usd" in rec and rec.get("unconfirmed"):
            out.append("<i>Себестоимость предварительная — есть неподтверждённые суммы.</i>")
    else:
        out.append("\nРасходов в таблице пока нет.")
    if rec.get("unconfirmed"):
        out.append("")
        out.append(unconfirmed_html(rec))
    warnings = []
    if "calc_customs_check_diff" in rec:
        warnings.append(f"⚠️ Таможня (AC) не равна сумме Y+Z+AA+AB: разница {uzs(rec['calc_customs_check_diff'])}")
    if "calc_total_check_diff" in rec:
        warnings.append(f"⚠️ Итог по таблице (BG) отличается от расчёта на {usd(rec['calc_total_check_diff'])}")
    if "calc_rate_used" not in rec:
        warnings.append("ℹ️ Нет курса (S/H) — себестоимость не рассчитана")
    if warnings:
        out.append("")
        out.extend(warnings)
    if rec["missing"]:
        out.append(f"\n❗ <b>Не заполнено: {len(rec['missing'])}</b>")
        out.append("<blockquote expandable>" + "\n".join(f"• {esc(m)}" for m in rec["missing"]) + "</blockquote>")
    return "\n".join(out)


def card_image_data(rid: int) -> dict | None:
    """Цифры для фирменной картинки груза (подписи по-русски, brand.py переводит)."""
    row, rec = app.db.get(rid), app.by_id.get(rid)
    if rec is None:
        return None
    br = bd.breakdown([rec])
    types = [(bd.TYPE_LABEL[k], v["usd"]) for k, v in br["by_type"].items() if v["usd"] > 0]
    types.sort(key=lambda x: -x[1])
    cats = cargo_keys(rid) or {}
    return {
        "title": row["title"], "status": STATUS_NAME[row["status"]], "status_code": row["status"],
        "resp": app.resp_map.get(rid, ""), "factory": num(rec, "factory_usd") or 0,
        "overhead": rec.get("calc_overhead_usd"), "landed": rec.get("calc_landed_usd"),
        "pct": rec.get("calc_overhead_pct"), "types": types[:6], "missing": rec["missing"],
        "unconfirmed": len(rec.get("unconfirmed", [])), "gtd": rec["gtd"] or "",
        "regime": cats.get("m", ("", ""))[1], "country": cats.get("c", ("", ""))[1],
        "transport": cats.get("t", ("", ""))[1], "sender": rec["sender"] or "", "row": sh.rows_label(rec),
        "days": days_open(row) if row["status"] in OPEN_STATUSES else None,
        "prelim": bool([m for m in rec["missing"] if "Счёт-фактура" not in m] or rec.get("unconfirmed")),
    }


def details_html(rid: int) -> str:
    """Все заполненные поля груза по разделам (раскрывающиеся блоки)."""
    row = app.db.get(rid)
    rec = app.by_id.get(rid)
    out = [f"📦 <b>{esc(row['title'])}</b> · подробно", status_line(row)]
    if rec is None:
        return "\n".join(out + ["\nℹ️ Этой записи больше нет в таблице."])
    for title, keys in sh.SECTIONS:
        items = []
        for k in keys:
            if sh.filled(rec, k):
                value = sh.fmt_value(k, rec[k])
                items.append(f"{esc(sh.META[k][0])}: <b>{esc(value[:120])}</b>")
        if items:
            out.append(f"\n<b>{esc(title)}</b>")
            out.append("<blockquote expandable>" + "\n".join(items) + "</blockquote>")
    text = "\n".join(out)
    return text if len(text) < 4000 else text[:3900].rsplit("\n", 1)[0] + "</blockquote>\n…"


# ===========================================================================
# Клавиатуры
# ===========================================================================
MENU_TODAY = "🏠 Сегодня"
MENU_CATS = "🗂 Категории"
MENU_CHECKS = "🧪 Проверки"
MENU_OPEN = "📋 Открытые работы"
MENU_OVERDUE = "⏰ Просроченные"
MENU_DONE = "✅ Завершённые"
MENU_SEARCH = "🔍 Поиск"
MENU_TOTALS = "📊 Итоги"
MENU_HELP = "📖 Справочник"
MENU_TEAM = "👥 Команда"
MENU_SUPPORT = "🆘 Поддержка"
MENU_LANG = "🌐 Язык"
MENU_MORE = "🏠 Меню"
MENU_REMARKS = "📌 Замечания"
MENU_DOCS = "📎 Документы"


def main_menu(user_id: int | None = None) -> ReplyKeyboardMarkup:
    """Короткое меню внизу экрана — своё у каждой роли. Всё остальное — в «☰ Ещё»."""
    role = role_of(user_id) if user_id is not None else None
    K = lambda t: KeyboardButton(text=t)
    if role is None or not can_edit(user_id):
        rows = [[K(MENU_TODAY), K(MENU_CATS)], [K(MENU_SEARCH), K(MENU_MORE)]]
        if role == VIEWER and has_access(user_id, "docs"):
            rows = [[K(MENU_TODAY), K(MENU_CATS)], [K(MENU_DOCS), K(MENU_SEARCH)], [K(MENU_MORE)]]
    elif role in (DIRECTOR, ADMIN) and can_manage(user_id):
        rows = [[K(MENU_TODAY), K(MENU_OPEN)], [K(MENU_REMARKS), K(MENU_DOCS)], [K(MENU_SEARCH), K(MENU_SUPPORT)],
                [K(MENU_MORE)]]
    elif has_access(user_id, "docs"):   # Разработчик, 1С УНФ-Оператор, Специалист по таможне, Сотрудник
        rows = [[K(MENU_TODAY), K(MENU_OPEN)], [K(MENU_DOCS), K(MENU_SEARCH)], [K(MENU_SUPPORT), K(MENU_MORE)]]
    else:
        rows = [[K(MENU_TODAY), K(MENU_OPEN)], [K(MENU_SEARCH), K(MENU_SUPPORT)], [K(MENU_MORE)]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True,
                               input_field_placeholder="Груз, ГТД, номер машины…")


def home_view(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    """🏠 Главное меню: короткая сводка и все разделы крупными кнопками."""
    rows_open = app.db.open_records()
    counts = {k: len(filtered_open(k)) for k in ("k", "u", "d", "o")}
    remarks = len(app.db.open_remarks())
    role = role_of(uid)
    out = ["🟠 <b>Uyda · Импорт</b> — главное меню",
           f"<i>{roles_label(uid)}</i>" if role else "", "",
           f"📋 Открытые работы: <b>{len(rows_open)}</b>",
           f"🛃 у таможни <b>{counts['k']}</b> · 🧾 у 1С УНФ <b>{counts['u']}</b>"
           + (f" · ⏳ срок прошёл <b>{counts['d']}</b>" if counts["d"] else "")
           + (f" · 📌 замечаний <b>{remarks}</b>" if remarks else ""),
           "", "Выберите раздел:"]
    kb = [[btn("🏠 Сегодня", "td"), btn("📋 Открытые работы", "l:a:0")]]
    mine = []
    if has_role(uid, CUSTOMS):
        mine.append(btn(f"🛃 Мои грузы · {counts['k']}", "l:k:0"))
    if has_role(uid, OPERATOR):
        mine.append(btn(f"🧾 Мои грузы · {counts['u']}", "l:u:0"))
    if counts["d"]:
        mine.append(btn(f"⏳ Срок прошёл · {counts['d']}", "l:d:0"))
    if mine:
        kb.append(mine)
    new_docs = len(app.db.docs_unacked(uid))
    if new_docs:
        kb.append([btn(f"🔔 Новые документы · {new_docs}", "dn")])
    rc = risk_counts()
    kb.append([btn(f"⚠️ Риски · 🔴 {rc['red']} · 🟠 {rc['orange']}", "rk:a:0")])
    _, more = more_view(uid)
    kb += more.inline_keyboard
    kb.append([btn("🔄 Обновить", "hm")])   # на главном меню «Назад» не нужен
    return "\n".join(x for x in out if x is not None), InlineKeyboardMarkup(inline_keyboard=kb)


NAV_SKIP = ("lg:", "zz:", "tr:", "sp:w", "sp:r", "sp:m", "sp:c", "sp:n", "rm:", "ra:", "u1:", "ci:", "xl:", "tdi",
            "mn:week", "noop", "s:", "f:", "z:", "rs:", "rp:", "ro:", "rn", "xl:r",
            "dg:", "da:", "dx:", "dw:", "ds:", "dp:", "dq", "dz", "dd", "df:", "dy:", "ut:", "ur:", "db")   # действия, а не экраны — в путь «Назад» не пишем


def nav_row() -> list[InlineKeyboardButton]:
    return [btn("◀ Назад", "bk"), btn("🏠 Меню", "hm")]


def with_nav(markup):
    """Добавить внизу «◀ Назад · 🏠 Меню», если такого ряда ещё нет."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    datas = {b.callback_data for row in markup.inline_keyboard for b in row}
    if "hm" in datas or any((d or "").startswith("lg:") for d in datas):
        return markup
    return markup.model_copy(update={"inline_keyboard": list(markup.inline_keyboard) + [nav_row()]})


def more_view(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    """«☰ Ещё»: все разделы кнопками в одном сообщении."""
    rows = [[btn("🗂 Категории", "k:a"), btn("✅ Завершённые", "mn:done")],
            [btn("📊 Итоги", "mn:tot"), btn("🧪 Проверки", "x")],
            [btn("⏰ Просроченные", "l:o:0"), btn("📌 Замечания", "mn:rem")],
            [btn("⚠️ Риски", "rk:a:0"), btn("📎 Документы", "dn")],
            [btn("📊 Дашборд", "db")]]
    if can_edit(uid):
        rows.append([btn("🆘 Поддержка", "mn:sup"), btn("📖 Справочник", "mn:help")])
    else:
        rows.append([btn("📖 Справочник", "mn:help")])
    tech = []
    if has_access(uid, "week"):
        rows.append([btn("📈 Отчёт за неделю", "mn:week")])
    if can_manage(uid):
        tech.append(btn("👥 Команда", "tm:0"))
    if is_admin(uid):
        tech.append(btn("🗒 Журнал", "j:0"))
    if tech:
        rows.append(tech)
    rows.append([btn("🌐 Til · Язык · Language", "mn:lang")])
    return "☰ <b>Все разделы</b>\nВыберите раздел:", InlineKeyboardMarkup(inline_keyboard=rows)
    # (используется и как часть главного меню)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def search_state(user_id: int) -> tuple[str, list[int], list[list[int]]]:
    """Последний поиск пользователя: запрос, найденные id, группы частей одного груза."""
    return app.searches.get(user_id, ("", [], []))


def group_page(user_id: int, rec_id: int) -> int:
    for gi, g in enumerate(search_state(user_id)[2]):
        if rec_id in g:
            return gi // PAGE_SIZE
    return 0


def ctx_ids(ctx: str, user_id: int) -> list[int]:
    """Грузы, среди которых листаем карточки ◀ ▶."""
    if ctx.startswith("l."):
        return [r["id"] for r in filtered_open(ctx.split(".")[1])]
    if ctx.startswith("q."):
        return search_state(user_id)[1]
    if ctx.startswith("g."):
        groups = search_state(user_id)[2]
        gi = int(ctx.split(".")[1])
        return groups[gi] if gi < len(groups) else []
    return app.order


def ctx_back(ctx: str, user_id: int = 0) -> InlineKeyboardButton | None:
    if ctx.startswith("l."):
        _, f, p = ctx.split(".")
        return btn("☰ К списку", f"l:{f}:{p}")
    if ctx.startswith("q."):
        back = app.search_back.get(user_id)
        return btn("☰ К списку", back) if back else btn("☰ К результатам", f"q:{ctx.split('.')[1]}")
    if ctx.startswith("g."):
        return btn("📦 Весь груз (все части)", f"g:{ctx.split('.')[1]}")
    return None


def neighbour_ctx(ctx: str, ids: list[int], new_index: int, user_id: int = 0) -> str:
    """При переходе ◀ ▶ номер страницы списка тоже переезжает, чтобы «К списку» вёл на нужную."""
    if ctx.startswith("l."):
        _, f, _ = ctx.split(".")
        return f"l.{f}.{new_index // PAGE_SIZE}"
    if ctx.startswith("q."):
        return f"q.{group_page(user_id, ids[new_index])}"
    return ctx


def card_kb(rid: int, ctx: str, user_id: int, details: bool = False, money: bool = False,
            exp: bool = False) -> InlineKeyboardMarkup:
    status = app.db.get(rid)["status"]
    rows = []
    if can_edit(user_id):   # Наблюдатель статусы не меняет — кнопок нет
        r1 = []
        if status != WORK:
            r1.append(btn("🔧 В работе", f"s:{rid}:work:{ctx}"))
        if status != DONE:
            r1.append(btn("✅ Завершён", f"s:{rid}:done:{ctx}"))
        if r1:
            rows.append(r1)
        rows.append([btn("⚠️ Причина", f"f:{rid}:{ctx}"), btn("⏰ Позже", f"z:{rid}:{ctx}")])
    else:
        rows.append([btn("⏰ Позже", f"z:{rid}:{ctx}")])
    brief = not (details or money or exp)
    if brief:
        extra = []
        if can_manage(user_id):
            extra.append(btn("📌 Замечание", f"rm:{rid}:{ctx}"))
        if can_edit(user_id):
            if app.db.open_remarks(rid):
                extra.append(btn("👍 Принято", f"ra:{rid}:{ctx}"))
            extra.append(btn("🆘 Помощь", f"sp:n:{rid}"))
        if extra:
            rows.append(extra)
    view = [] if brief else [btn("📦 Кратко", f"c:{rid}:{ctx}")]
    if not exp:
        view.append(btn("💵 Расходы", f"e:{rid}:{ctx}"))
    if not details:
        view.append(btn("📄 Подробно", f"d:{rid}:{ctx}"))
    if not money:
        view.append(btn("💱 Валюты", f"v:{rid}:{ctx}"))
    rows.append(view)
    last = [btn("🧾 Для 1С", f"u1:{rid}")] if can_edit(user_id) else []
    if brand.AVAILABLE:
        last.append(btn("🖼 Картинкой", f"ci:{rid}"))
    if brief and last:
        rows.append(last)
    if brief:
        total, waiting = app.db.doc_counts(doc_cargo(rid)[1])
        rows.append([btn(f"📎 Документы · {total}" + (" 🔔" if waiting else "") if total else "📎 Документы",
                         f"dc:{rid}:0")])

    ids = ctx_ids(ctx, user_id)
    nav = []
    if rid in ids:
        i = ids.index(rid)
        nav.append(btn("◀ Пред.", f"c:{ids[i - 1]}:{neighbour_ctx(ctx, ids, i - 1, user_id)}") if i > 0 else btn(" ", "noop"))
        nav.append(btn(f"{i + 1} / {len(ids)}", "noop"))
        nav.append(btn("След. ▶", f"c:{ids[i + 1]}:{neighbour_ctx(ctx, ids, i + 1, user_id)}")
                   if i < len(ids) - 1 else btn(" ", "noop"))
        rows.append(nav)
    cats = cargo_keys(rid)
    if cats and brief:
        rows.append([btn(short_title(cats["c"][1], 18), f"kc:c:{cats['c'][0]}:0:a"),
                     btn("🏭 " + short_title(cats["s"][1], 18), f"kc:s:{cats['s'][0]}:0:a")])
    back = ctx_back(ctx, user_id)
    if back:
        rows.append([back])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def snooze_kb(rid: int, ctx: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("Через 1 час", f"zz:{rid}:1h:{ctx}"), btn("Через 3 часа", f"zz:{rid}:3h:{ctx}")],
        [btn(f"Завтра в {cfg.DAILY_REMINDER_TIME}", f"zz:{rid}:tm:{ctx}")],
        [btn("← Назад", f"c:{rid}:{ctx}")],
    ])


def item_button(rec_id: int, ctx: str) -> InlineKeyboardButton:
    row = app.db.get(rec_id)
    rec = app.by_id.get(rec_id)
    text = f"{STATUS_ICON[row['status']]} {short_title(row['title'], 26)}"
    if row["status"] in OPEN_STATUSES:
        d = days_open(row)
        text += f" · {plural_days(d)}" + (" ⏰" if d >= cfg.OVERDUE_DAYS else "")
    if rec and rec.get("unconfirmed"):
        text += " · 🟡"
    if rec and rec["missing"]:
        text += f" · ❗{len(rec['missing'])}"
    return btn(text, f"c:{rec_id}:{ctx}")


def pager(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    return [btn("‹ Назад", f"{prefix}:{page - 1}") if page > 0 else btn(" ", "noop"),
            btn(f"{page + 1} / {pages}", "noop"),
            btn("Вперёд ›", f"{prefix}:{page + 1}") if page < pages - 1 else btn(" ", "noop")]


# ===========================================================================
# Экраны: списки, итоги, сводка
# ===========================================================================
FILTERS = {"a": ("Все", None), "n": ("🆕", NEW), "w": ("🔧", WORK), "f": ("⚠️", FAIL), "o": ("⏰", "overdue"),
           "y": ("🟡", "yellow"), "m": ("❗", "missing"), "k": ("🛃", "customs"), "u": ("🧾", "unf"),
           "d": ("⏳", "sla")}
FILTER_TITLES = {"a": "Открытые работы", "n": "Новые", "w": "В работе", "f": "Не завершены",
                 "o": f"Просроченные (дольше {cfg.OVERDUE_DAYS} дн.)", "y": "Не подтверждено (жёлтые ячейки)",
                 "m": "Не заполнены расходы",
                 "k": "У Специалиста по таможенному оформлению (есть незаполненное)",
                 "u": "У 1С УНФ-Оператора (всё заполнено, груз не завершён)",
                 "d": "Срок этапа прошёл"}


def filtered_open(f: str) -> list:
    rows = app.db.open_records()
    kind = FILTERS.get(f, FILTERS["a"])[1]
    if kind == "overdue":
        return [r for r in rows if days_open(r) >= cfg.OVERDUE_DAYS]
    if kind == "sla":
        return [r for r in rows if stage_overdue(r["id"])]
    if kind == "customs":
        return [r for r in rows if app.resp_map.get(r["id"]) == CUSTOMS]
    if kind == "unf":
        return [r for r in rows if app.resp_map.get(r["id"]) == OPERATOR]
    if kind == "yellow":
        return [r for r in rows if app.by_id.get(r["id"]) and app.by_id[r["id"]].get("unconfirmed")]
    if kind == "missing":
        return [r for r in rows if app.by_id.get(r["id"])
                and any("Счёт-фактура" not in m for m in app.by_id[r["id"]]["missing"])]
    if kind:
        return [r for r in rows if r["status"] == kind]
    return rows


def list_view(f: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    all_rows = app.db.open_records()
    rows = filtered_open(f)
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    counts = {k: len(filtered_open(k)) for k in FILTERS}

    text = [f"📋 <b>{FILTER_TITLES[f]}</b> · {len(rows)}"]
    if f == "a" and all_rows:
        text.append(f"🆕 {counts['n']} · 🔧 {counts['w']} · ⚠️ {counts['f']} · ⏰ {counts['o']} · "
                    f"❗ {counts['m']} · 🟡 {counts['y']}")
        text.append(f"🛃 у таможни {counts['k']} · 🧾 у 1С УНФ {counts['u']}")
    text.append("")
    text.append("Нажмите на груз, чтобы открыть карточку." if rows else "Здесь пусто 👍")

    keys = list(FILTERS)
    kb = [[btn(("✓ " if k == f else "") + f"{FILTERS[k][0]} {counts[k]}", f"l:{k}:0") for k in keys[i:j]]
          for i, j in ((0, 4), (4, 7), (7, 10))]
    for r in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        kb.append([item_button(r["id"], f"l.{f}.{page}")])
    if pages > 1:
        kb.append(pager(f"l:{f}", page, pages))
    kb.append([btn("📥 Excel", f"xl:l:{f}"), btn("🔄 Обновить", f"r:{f}:{page}")])
    return "\n".join(text), InlineKeyboardMarkup(inline_keyboard=kb)


def parts_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} часть"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} части"
    return f"{n} частей"


def money_line(rec: dict) -> str:
    parts = []
    if num(rec, "factory_usd"):
        parts.append(f"завод {money(rec['factory_usd'])}")
    if num(rec, "calc_overhead_usd"):
        parts.append(f"расходы {money(rec['calc_overhead_usd'])}")
    if num(rec, "calc_landed_usd"):
        parts.append(f"итого <b>{money(rec['calc_landed_usd'])}</b>")
    return " · ".join(parts)


def group_recs(user_id: int, gi: int) -> list[dict]:
    groups = search_state(user_id)[2]
    if gi >= len(groups):
        return []
    return [app.by_id[i] for i in groups[gi] if i in app.by_id]


def results_view(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    query, ids, groups = search_state(user_id)
    pages = max(1, (len(groups) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    recs = [app.by_id[i] for i in ids if i in app.by_id]
    total = sh.aggregate(recs) if recs else {}

    head = f"🔍 <b>Найдено грузов: {len(groups)}</b>"
    if len(ids) != len(groups):
        head += f" ({len(ids)} строк)"
    out = [head + f" · «{esc(query)}»"]
    summary = [(label, usd(total[k])) for k, label in (("factory_usd", "Цена завода"), ("calc_overhead_usd", "Расходы"),
                                                       ("calc_landed_usd", "Себестоимость")) if num(total, k)]
    if summary:
        out.append(table(summary))

    kb = []
    for gi in range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(groups))):
        g = group_recs(user_id, gi)
        if not g:
            continue
        agg = sh.aggregate(g) if len(g) > 1 else g[0]
        name = sh.display_name(g[0]["title"]) if len(g) > 1 else g[0]["title"]
        line = f"\n{gi + 1}. <b>{esc(name)}</b>" + (f" · {parts_word(len(g))}" if len(g) > 1 else "")
        ml = money_line(agg)
        out.append(line + (f"\n    {ml}" if ml else ""))
        if len(g) == 1:
            kb.append([item_button(g[0]["id"], f"q.{page}")])
        else:
            kb.append([btn(f"📦 {short_title(name, 26)} · {parts_word(len(g))}", f"g:{gi}")])
    out.append("\nНажмите на груз, чтобы открыть цену завода и все статьи расходов.")
    if pages > 1:
        kb.append(pager("q", page, pages))
    return "\n".join(out), InlineKeyboardMarkup(inline_keyboard=kb)


def group_view(user_id: int, gi: int) -> tuple[str, InlineKeyboardMarkup]:
    """Весь груз: все части с одинаковым названием (разные написания, (1-4), ИМ74) — суммой и по частям."""
    g = group_recs(user_id, gi)
    if not g:
        return "Результаты поиска устарели — повторите поиск.", InlineKeyboardMarkup(inline_keyboard=[])
    agg = sh.aggregate(g)
    name = sh.display_name(g[0]["title"])
    rows = sorted(r for p in g for r in p["_rows"])
    out = [f"📦 <b>{esc(name)}</b> · {parts_word(len(g))}",
           f"📄 Строки таблицы: {rows[0]}–{rows[-1]}" if len(rows) > 1 else f"📄 Строка таблицы: {rows[0]}"]
    senders = {p["sender"] for p in g if p["sender"]}
    if len(senders) == 1:
        out.append(f"🏭 {esc(senders.pop())}")
    companies = {p["logistics_company"] for p in g if p["logistics_company"]}
    if companies:
        out.append(f"🚚 {esc(', '.join(sorted(companies)))}")

    money = money_rows(agg)
    if money:
        out.append(f"\n💵 <b>Цена завода и расходы — сумма всех частей</b>")
        out.append(table(money))

    out.append("\n<b>По частям</b>")
    for n, p in enumerate(g, 1):
        row = app.db.get(p["id"])
        line = f"{n}. {STATUS_ICON[row['status']]} <b>{esc(p['title'])}</b>"
        if p["gtd"]:
            line += f"\n    ГТД <code>{esc(p['gtd'])}</code>"
        ml = money_line(p)
        if ml:
            line += f"\n    {ml}"
        missing = [m for m in p["missing"] if "Счёт-фактура" not in m]
        if p.get("unconfirmed"):
            line += f"\n    🟡 не подтверждено: {len(p['unconfirmed'])}"
        if missing and num(p, "factory_usd"):
            line += f"\n    ❗ не заполнено: {len(p['missing'])}"
        out.append(line)
    out.append("\nНажмите на часть, чтобы открыть её карточку и отметить статус.")

    kb = [[item_button(p["id"], f"g.{gi}")] for p in g]
    kb.append([btn("💱 Валюты и виды — весь груз", f"gv:{gi}")])
    groups = search_state(user_id)[2]
    if len(groups) > 1:
        kb.append([btn("◀ Пред. груз", f"g:{gi - 1}") if gi > 0 else btn(" ", "noop"),
                   btn(f"{gi + 1} / {len(groups)}", "noop"),
                   btn("След. груз ▶", f"g:{gi + 1}") if gi < len(groups) - 1 else btn(" ", "noop")])
    back = app.search_back.get(user_id)
    if back:
        kb.append([btn("☰ К списку", back)])
    elif len(groups) > 1:
        kb.append([btn("☰ К результатам", f"q:{gi // PAGE_SIZE}")])
    text = "\n".join(out)
    if len(text) > 4000:
        text = text[:3900].rsplit("\n", 1)[0] + "\n…"
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


def im40_date(parts: list[dict]):
    """Дата груза — по ГТД выпуска (ИМ40), а не по ГТД склада (ИМ70/ИМ74). Если частей несколько — последняя."""
    dates = [p["gtd_date"] for p in parts if p.get("gtd_date") and not IM_WAREHOUSE.search(p["title"])]
    dates = dates or [p["gtd_date"] for p in parts if p.get("gtd_date")]
    return max(dates) if dates else None


def month_label(ym: str) -> str:
    if ym == "none":
        return "Без даты ГТД"
    y, m = ym.split("-")
    return f"{MONTHS[int(m) - 1]} {y}"


def closed_groups() -> list[tuple[object, list[dict]]]:
    """Завершённые грузы (✅): части одного груза вместе, с датой по ГТД ИМ40, новые сверху."""
    closed = [r for r in app.records if r.get("id") and app.db.get(r["id"])
              and app.db.get(r["id"])["status"] in CLOSED_STATUSES]
    items = [(im40_date(g), g) for g in sh.group_by_base(closed)]
    return sorted(items, key=lambda x: (x[0] is not None, x[0] or datetime.min.date()), reverse=True)


def done_view(user_id: int, ym: str = "", page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    items = closed_groups()
    by_month: dict[str, list] = {}
    for d, g in items:
        by_month.setdefault(d.strftime("%Y-%m") if d else "none", []).append((d, g))
    months = list(by_month)  # уже по убыванию даты, «без даты» — в конце
    if not months:
        return "✅ <b>Завершённые работы</b>\n\nПока нет завершённых грузов.", InlineKeyboardMarkup(inline_keyboard=[])
    ym = ym if ym in by_month else months[0]
    mi = months.index(ym)
    month_items = by_month[ym]
    pages = max(1, (len(month_items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)

    # Для карточек и «весь груз» с кнопками ◀ ▶ и «☰ К списку»
    groups_ids = [[r["id"] for r in g] for _, g in month_items]
    app.searches[user_id] = (f"Завершённые · {month_label(ym)}", [i for g in groups_ids for i in g], groups_ids)
    app.search_back[user_id] = f"dn:{ym}:{page}"

    all_recs = [r for _, g in month_items for r in g]
    total = sh.aggregate(all_recs)
    out = [f"✅ <b>Завершённые работы · {month_label(ym)}</b>",
           f"Грузов: <b>{len(month_items)}</b> · всего завершено: {len(items)}",
           "<i>Дата — по ГТД выпуска (ИМ40)</i>"]
    summary = [(label, usd(total[k])) for k, label in (("factory_usd", "Цена завода"), ("calc_overhead_usd", "Расходы"),
                                                       ("calc_landed_usd", "Себестоимость")) if num(total, k)]
    if summary:
        out.append(table(summary))

    kb = []
    for gi in range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(month_items))):
        d, g = month_items[gi]
        agg = sh.aggregate(g) if len(g) > 1 else g[0]
        name = sh.display_name(g[0]["title"]) if len(g) > 1 else g[0]["title"]
        date_s = d.strftime("%d.%m.%Y") if d else "—"
        line = f"\n{gi + 1}. 📅 {date_s} · <b>{esc(name)}</b>" + (f" · {parts_word(len(g))}" if len(g) > 1 else "")
        if agg.get("unconfirmed"):
            line += " · 🟡"
        ml = money_line(agg)
        out.append(line + (f"\n    {ml}" if ml else ""))
        label = f"✅ {date_s[:5]} · {short_title(name, 24)}" + (f" · {parts_word(len(g))}" if len(g) > 1 else "")
        kb.append([btn(label, f"g:{gi}") if len(g) > 1 else btn(label, f"c:{g[0]['id']}:q.{page}")])
    out.append("\nНажмите на груз — цена завода и все статьи расходов.")
    if pages > 1:
        kb.append(pager(f"dn:{ym}", page, pages))
    # Переход по месяцам и годам (месяцы идут от новых к старым)
    older = months[mi + 1] if mi + 1 < len(months) else None
    newer = months[mi - 1] if mi > 0 else None
    kb.append([btn(f"‹ {month_label(older)}", f"dn:{older}:0") if older else btn(" ", "noop"),
               btn(f"{month_label(newer)} ›", f"dn:{newer}:0") if newer else btn(" ", "noop")])
    if ym != "none":
        y = int(ym[:4])
        prev_year = next((m for m in months if m != "none" and int(m[:4]) < y), None)
        next_year = next((m for m in reversed(months) if m != "none" and int(m[:4]) > y), None)
        kb.append([btn(f"« {prev_year[:4]}", f"dn:{prev_year}:0") if prev_year else btn(" ", "noop"),
                   btn("📅 Последний месяц", f"dn:{months[0]}:0"),
                   btn(f"{next_year[:4]} »", f"dn:{next_year}:0") if next_year else btn(" ", "noop")])
    text = "\n".join(out)
    if len(text) > 4000:
        text = text[:3900].rsplit("\n", 1)[0] + "\n…"
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


PERIODS = {"open": "Открытые работы", "m": "Текущий месяц", "y": "Текущий год", "all": "Всё время"}


def period_kb(current: str = "", money: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [btn(("✓ " if k == current else "") + v, f"t:{k}") for k, v in list(PERIODS.items())[:2]],
        [btn(("✓ " if k == current else "") + v, f"t:{k}") for k, v in list(PERIODS.items())[2:]],
    ]
    if current:
        rows.append([btn("📊 Итоги", f"t:{current}") if money else btn("💱 Валюты и виды", f"tv:{current}"),
                     btn("🗂 По категориям", "k:y" if current in ("y", "m") else "k:a")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def records_for_period(period: str) -> list[dict]:
    today = datetime.now(cfg.TZ).date()
    if period == "open":
        return [app.by_id[r["id"]] for r in app.db.open_records() if r["id"] in app.by_id]
    if period == "m":
        return [r for r in app.records if r.get("gtd_date") and (r["gtd_date"].year, r["gtd_date"].month) == (today.year, today.month)]
    if period == "y":
        return [r for r in app.records if r.get("gtd_date") and r["gtd_date"].year == today.year]
    return list(app.records)


def totals_html(records: list[dict], title: str) -> str:
    def s(key, subset=None):
        return sum(r[key] for r in (subset if subset is not None else records) if isinstance(r.get(key), (int, float)))

    rows: list[tuple[str, str] | None] = [("Грузов", str(len(records)))]
    for key, label, kind in (("factory_usd", "Завод", "usd"), ("paid_goods_usd", "Банк", "usd"),
                             ("customs_total", "Таможня", "uzs"), ("vat_29", "  в т.ч. НДС", "uzs"),
                             ("calc_cert_total_uzs", "Сертификаты", "uzs"),
                             ("calc_logistics_total_usd", "Логистика", "usd"),
                             ("calc_other_total_usd", "Прочие", "usd")):
        v = s(key)
        if v:
            rows.append((label, usd(v) if kind == "usd" else uzs(v)))
    if s("calc_landed_usd"):
        rows.append(None)
        rows.append(("Себестоимость", usd(s("calc_landed_usd"))))
        rows.append(("Без НДС", usd(s("calc_landed_ex_vat_usd"))))
        with_calc = [r for r in records if "calc_overhead_pct" in r]
        if with_calc and s("factory_usd", with_calc):
            rows.append(("Накладные", sh.fmt_num(s("calc_overhead_usd", with_calc) / s("factory_usd", with_calc) * 100, 1) + " %"))
    if s("net_kg"):
        rows.append(("Нетто", f"{sh.fmt_num(s('net_kg'), 0)} кг"))

    out = [f"📊 <b>Итоги · {esc(title)}</b>", table(rows) if records else "Нет грузов за этот период."]
    incomplete = sum(1 for r in records if any("Счёт-фактура" not in m for m in r["missing"]))
    if incomplete:
        out.append(f"❗ С незаполненными расходами: <b>{incomplete}</b> — суммы предварительные.")
    no_rate = sum(1 for r in records if "calc_rate_used" not in r)
    if no_rate:
        out.append(f"ℹ️ Без курса (не посчитаны): {no_rate}")
    return "\n".join(out)


def source_info() -> tuple[str, bool]:
    """Откуда бот берёт данные и не устарели ли они: (строка для сообщения, нужно ли предупреждение)."""
    if not cfg.EXCEL_FILE:
        return "Google Таблица", False
    info = rel.file_age(cfg.EXCEL_FILE)
    if info is None:
        return f"файл <b>{esc(cfg.EXCEL_FILE)}</b> не найден в папке бота", True
    mtime, age = info
    stale = bool(cfg.EXCEL_STALE_DAYS) and age > cfg.EXCEL_STALE_DAYS * 86400
    return f"Excel-файл <b>{esc(cfg.EXCEL_FILE)}</b>, обновлён {fmt_dt(int(mtime))} ({rel.human_duration(age)} назад)", stale


def health_lines() -> list[str]:
    """Пульс бота для утренней сводки и /status: жив ли бот, читается ли таблица, свежий ли файл."""
    w = app.watch
    src, stale = source_info()
    if w.is_down:
        out = [f"🔴 <b>Таблица не читается</b> уже {rel.human_duration(time.time() - w.down_since)}",
               f"<blockquote>{esc(w.last_error or 'причина неизвестна')}</blockquote>"]
    else:
        read = fmt_dt(int(w.last_ok)) if w.last_ok else "—"
        out = [f"🟢 Бот работает · таблица прочитана {read}"]
    out.append(f"📄 Источник: {src}")
    if stale:
        out.append("⚠️ Файл давно не обновлялся — данные могут быть устаревшими. "
                   "Положите свежую выгрузку в папку бота и нажмите /reload.")
    return out


def status_html() -> str:
    """Подробное состояние бота — команда /status (Директору и Разработчику)."""
    last_backup = app.db.get_meta("last_backup")
    backups = rel.list_backups(cfg.BACKUP_DIR)
    open_rows = app.db.open_records()
    out = ["🩺 <b>Состояние бота</b>", ""] + health_lines() + [
        "",
        f"📋 Грузов в таблице: <b>{len(app.records)}</b> · открытых работ: <b>{len(open_rows)}</b>",
        f"🤖 Версия: {BOT_VERSION} · запущен {fmt_dt(int(app.started))} (работает {rel.human_duration(time.time() - app.started)})",
        f"💾 База: {esc(cfg.DB_FILE)}, {rel.human_size(app.db.file_size())}",
        f"🗄 Последняя автокопия: {fmt_dt(int(last_backup)) if last_backup else 'ещё не делалась'} "
        f"· в папке {esc(cfg.BACKUP_DIR)}: {len(backups)} из {cfg.BACKUP_KEEP}",
        f"⏱ Проверка таблицы каждые {cfg.SYNC_INTERVAL_SECONDS // 60 or 1} мин. · тревога, если не читается {cfg.SHEET_ALERT_MINUTES} мин.",
        f"☀️ Сводка в {cfg.DAILY_REMINDER_TIME} · копия базы в {cfg.BACKUP_TIME}"
        + (" · по понедельникам файлом в Telegram" if cfg.BACKUP_TO_ADMINS else ""),
        f"📡 Внешний пульс: {'включён' if cfg.HEALTHCHECK_URL else 'не настроен'}",
        f"👥 Получателей уведомлений: {len(recipients())}",
    ]
    return "\n".join(out)


def digest_data() -> dict:
    """Цифры для фирменной картинки-сводки (подписи — по-русски, brand.py переводит)."""
    rows = app.db.open_records()
    counts = {k: len(filtered_open(k)) for k in FILTERS}
    remarks = len(app.db.open_remarks())
    kpi = [("Открытые работы", len(rows), "ink"), ("🛃 У таможни", counts["k"], "orange"),
           ("🧾 У 1С УНФ", counts["u"], "teal"), ("Просрочено", counts["o"], "red" if counts["o"] else "muted"),
           ("Не подтверждено", counts["y"], "amber" if counts["y"] else "muted"),
           ("📌 Замечаний открыто", remarks, "red" if remarks else "muted")]
    kpi = [(lbl.split(" ", 1)[1] if lbl[0] in "🛃🧾📌" else lbl, v, c) for lbl, v, c in kpi]   # без эмодзи на картинке
    att = []
    for r in sorted(rows, key=lambda r: r["opened_at"])[:5]:
        att.append((r["title"], days_open(r), app.resp_map.get(r["id"], "")))
    now = datetime.now(cfg.TZ)
    months = []
    y, m = now.year, now.month
    for _ in range(12):
        months.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.reverse()
    by = {}
    for c in all_cargos():
        if c["date"]:
            by[(c["date"].year, c["date"].month)] = by.get((c["date"].year, c["date"].month), 0) + 1
    return {"date": now, "kpi": kpi, "attention": att, "overdue": cfg.OVERDUE_DAYS,
            "months": [(yy, mm, by.get((yy, mm), 0)) for yy, mm in months]}


def digest_html() -> tuple[str, InlineKeyboardMarkup]:
    rows = app.db.open_records()
    now = int(time.time())
    done = app.db.done_since(now - 86400)
    today = datetime.now(cfg.TZ).strftime("%d.%m.%Y")
    out = [f"☀️ <b>Сводка на {today}</b>", ""] + health_lines() + [""]
    if not rows:
        out.append("Открытых работ нет 👍")
    else:
        counts = {s: sum(1 for r in rows if r["status"] == s) for s in OPEN_STATUSES}
        overdue = [r for r in rows if days_open(r) >= cfg.OVERDUE_DAYS]
        missing = filtered_open("m")   # без пустых счёт-фактур — как фильтр ❗ в списке
        out.append(f"📋 Открытых работ: <b>{len(rows)}</b>")
        out.append(f"🆕 новые {counts[NEW]} · 🔧 в работе {counts[WORK]} · ⚠️ не завершены {counts[FAIL]}")
        if overdue:
            out.append(f"⏰ Просрочено (дольше {cfg.OVERDUE_DAYS} дн.): <b>{len(overdue)}</b>")
        if missing:
            out.append(f"❗ С незаполненными расходами: <b>{len(missing)}</b>")
        yellow = [r for r in rows if app.by_id.get(r["id"]) and app.by_id[r["id"]].get("unconfirmed")]
        if yellow:
            out.append(f"🟡 С неподтверждёнными суммами: <b>{len(yellow)}</b>")
        out.append(f"🛃 У таможни: <b>{len(filtered_open('k'))}</b> · 🧾 У 1С УНФ: <b>{len(filtered_open('u'))}</b>")
        rc = risk_counts()
        if rc["red"] or rc["orange"]:
            out.append(f"⚠️ Риски: 🔴 <b>{rc['red']}</b> · 🟠 <b>{rc['orange']}</b>")
            top = [r for r in app.db.risks() if r["p"] * r["i"] >= 8][:3]
            out += [f"   {rk.level(r['p'] * r['i'])[0]} {esc(rk.KINDS.get(r['kind'], ('', 'Риск'))[1])} — "
                    f"{esc(short_title(r['title'], 30))}" for r in top]
        if app.db.open_remarks():
            out.append(f"📌 Замечаний без ответа: <b>{len(app.db.open_remarks())}</b>")
        attention = sorted(rows, key=lambda r: r["opened_at"])[:10]
        out.append("\n<b>Требуют внимания</b>")
        for i, r in enumerate(attention, 1):
            rec = app.by_id.get(r["id"])
            tail = (" · 🟡" if rec and rec.get("unconfirmed") else "") + (f" · ❗{len(rec['missing'])}" if rec and rec["missing"] else "")
            d = days_open(r)
            who = RESP_ICON.get(app.resp_map.get(r["id"], ""), "")
            out.append(f"{i}. {STATUS_ICON[r['status']]} {esc(r['title'])} — {plural_days(d)}{' ⏰' if d >= cfg.OVERDUE_DAYS else ''}{tail}{' · ' + who if who else ''}")
        reasons = [r for r in rows if r["status"] == FAIL and r["reason"]]
        if reasons:
            out.append("\n<b>Причины незавершения</b>")
            out.append("<blockquote expandable>" + "\n".join(
                f"• {esc(r['title'])}: {esc(r['reason'])}" for r in reasons) + "</blockquote>")
    if done:
        out.append(f"\n✅ Завершено за сутки: <b>{len(done)}</b>")
        out.append("<blockquote expandable>" + "\n".join(
            f"• {esc(r['title'])} — {esc(r['updated_by'])}" + (f" · ⚠️ {esc(row_warn(r))}" if row_warn(r) else "")
            for r in done[:30]) + "</blockquote>")
        warned = [r for r in done if row_warn(r)]
        if warned:
            out.append(f"⚠️ Из них закрыто с пометкой: <b>{len(warned)}</b>")
    found = checks_scan()
    issues = sum(len(found[k]) for k in ("bg", "rate", "high", "ac"))
    if issues:
        out.append(f"\n🧪 Проверки данных: <b>{issues}</b> грузов с вероятной ошибкой в таблице")
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🏠 Сегодня", "td"), btn("📋 Открыть список", "l:a:0")],
                                               [btn("⏰ Просроченные", "l:o:0"), btn("🧪 Проверки", "x")]])
    return "\n".join(out), kb


def open_from_text() -> str:
    """Где начинаются открытые работы — для справочника."""
    name = cfg.OPEN_FROM_CARGO
    if not name:
        return "все грузы, добавленные в таблицу после запуска бота"
    target = sh.normalize(name)
    rec = next((r for r in app.records if sh.normalize(r["title"]) == target), None)
    row = f" (строка {rec['_rows'][0]})" if rec else ""
    return f"с груза «{esc(name)}»{row} и ниже. Все грузы выше — завершены ✅"


def reference_html() -> str:
    """📖 Справочник: статусы, обозначения, правила таблицы, статьи расходов, команды."""
    return "\n".join([
        "📖 <b>Справочник</b>",
        "",
        "<b>Открытые работы</b>",
        f"Начинаются {open_from_text()}.",
        "",
        "<b>✅ Завершённые</b>",
        "Все остальные грузы из таблицы — по месяцам, новые сверху. Дата груза — по ГТД выпуска "
        "<b>ИМ40</b> (не по ГТД склада ИМ70/ИМ74). Листайте ‹ › по месяцам и « » по годам.",
        "",
        "<b>🏠 Сегодня</b>",
        "Главный экран: что горит, главные цифры, месяц к прошлому месяцу и кнопки на нужные списки.",
        "",
        "<b>🗂 Категории</b>",
        "<blockquote expandable>"
        "Все грузы, разделённые по столбцам таблицы: 🌍 страна отправления (I), "
        "📜 таможенный режим (метка ИМ40 / ИМ74 / ИМ70 / НД40 / ИМ51 в названии), 🏭 поставщик (B), "
        "🚛 транспорт (L), "
        "🏢 получатель (C), 📅 год по ГТД ИМ40, 📦 условия поставки (K), "
        "🛃 таможенный пост (первая часть номера ГТД, D).\n"
        "В каждой категории: число грузов и доля, цена завода, расходы, себестоимость, расходы к заводу "
        "(общая и «обычная» — медиана), цена за 1 кг, сравнение со всеми грузами, годы и список грузов.\n"
        "Можно просто написать в чат «Китай», «ПОСУДА», «2025», «FCA» или «ИМ74» — откроется категория."
        "</blockquote>",
        "<b>📜 Таможенные режимы</b>",
        "<blockquote expandable>"
        "Режим указан в названии груза (столбец A). Без метки — обычная декларация <b>ИМ40</b>.\n"
        "<b>ИМ40</b> — Импорт (выпуск для свободного обращения): здесь цена завода и все расходы\n"
        "<b>ИМ74</b> — Таможенный склад: до выпуска, в строке только небольшой сбор\n"
        "<b>ИМ70</b> — Временное хранение: до выпуска, в строке почти нет денег\n"
        "<b>НД40</b> — Неполная декларация (полный расчёт в строке есть)\n"
        "<b>ИМ51</b> — Переработка на таможенной территории\n"
        "ИМ74 и ИМ70 — «хвосты» того же груза: бот складывает их с ИМ40 в один груз."
        "</blockquote>",
        "<b>🧪 Проверки</b>",
        "<blockquote expandable>"
        "Бот сам ищет вероятные ошибки ввода: BG ≠ расчёт, курс сильно не такой, как обычно в этом месяце, "
        f"расходы больше {ck.OVERHEAD_PCT_MAX:.0f}% или меньше {ck.OVERHEAD_PCT_MIN:.0f}% цены завода, "
        "AC ≠ Y+Z+AA+AB, нет курса, нет цены завода, метка ИМ70/ИМ74 на полной декларации. "
        "Таблицу бот не меняет — только показывает, где посмотреть."
        "</blockquote>",
        "<b>Закрытие с пометкой</b>",
        "<blockquote expandable>"
        "Если у груза есть 🟡 жёлтые ячейки, ❗ пустые расходы или нет цены завода, бот перед закрытием "
        "спросит подтверждение. Такой груз отмечается «⚠️ с пометкой» — это видно Директору, в журнале и в сводке. "
        "Если в завершённом грузе появится новая жёлтая ячейка, он сам вернётся в 🆕 Новые."
        "</blockquote>",
        "<b>Статусы</b>",
        "<blockquote expandable>"
        "🆕 Новая — груз появился в таблице, его ещё никто не взял\n"
        "🔧 В работе — расходы оформляются\n"
        "✅ Завершён — всё оплачено и подтверждено\n"
        "⚠️ Не завершён — указана причина (видна в карточке и в сводке)\n"
        "Грузы выше начала открытых работ считаются завершёнными ✅"
        "</blockquote>",
        "<b>Кто отвечает за груз</b>",
        "<blockquote expandable>"
        "Бот назначает ответственного сам, по таблице:\n"
        "🛃 Специалист по таможенному оформлению — если в грузе есть незаполненные расходы (❗) или "
        "неподтверждённая жёлтая сумма (🟡);\n"
        "🧾 1С УНФ-Оператор — если всё заполнено, но груз не завершён;\n"
        "после завершения ответственного нет. Как только заполнено последнее поле, груз сам переходит "
        "к 1С УНФ — ему приходит уведомление; если в таблице что-то снова пусто — возвращается таможне.\n"
        "Фильтры 🛃 и 🧾 — в «📋 Открытые работы» и в «🏠 Сегодня»."
        "</blockquote>",
        "<b>Замечания и поддержка</b>",
        "<blockquote expandable>"
        "📌 Генеральный Директор на карточке груза нажимает «Замечание» и пишет текст — его получают "
        "ответственный, 1С УНФ-Оператор и Разработчик. Исполнитель нажимает «👍 Принято» — Директору приходит "
        "ответ. «📌 Замечания» в меню — список открытых.\n"
        "🆘 «Поддержка»: Оператор (или любой сотрудник) нажимает «Написать», пишет вопрос — Разработчик и Директор "
        "получают его сразу и отвечают кнопкой «Ответить» прямо в боте. Вопрос можно задать по конкретному грузу "
        "(кнопка «🆘 Помощь» на карточке). Директор видит всю переписку и может отправить замечание. "
        "«✅ Решено» закрывает обращение. /cancel — отменить ввод."
        "</blockquote>",
        "<b>📎 Документы партии</b>",
        "<blockquote expandable>"
        "Файлы (Excel, PDF, Word, фото) передаются через бота и хранятся у партии. Просто пришлите файл боту — "
        "он спросит, к какой партии его отнести и кому отправить: 🛃 Специалисту по таможне, 🧾 1С УНФ-Оператору, "
        "👔 Директору (можно нескольким сразу). Или на карточке партии: «📎 Документы» → «📤 Отправить файлы». "
        "Подпись к файлу станет комментарием.\n"
        "🟡 Рабочая версия — для работы между Специалистом по таможне и Оператором, Директору не уходит. "
        "✅ Финальная версия — файл готов: Директор получает его сразу. Отметить готовым можно при отправке "
        "или потом кнопкой «⭐ Сделать финальным». Файл с тем же названием — новая версия (v2, v3).\n"
        "Получатель получает сам файл и нажимает «✅ Принято» — отправителю приходит ответ. Кто не принял файл "
        "больше суток — получает напоминание утром. У партии видна вся история и фильтры: все, финальные, рабочие, "
        "мне, от меня; любой файл можно получить снова, все файлы — одним архивом, реестр — в Excel. "
        "Копии файлов хранятся в папке docs рядом с ботом."
        "</blockquote>",
        "<b>📊 Дашборд и 🔐 доступы</b>",
        "<blockquote expandable>"
        "«📊 Дашборд» — бот присылает файл-страницу: главные цифры, завод и расходы по месяцам, расходы к заводу, "
        "категории, открытые партии с поиском и сортировкой, риски, документы. Открывается в браузере, интернет не нужен.\n"
        "Генеральный Директор в «👥 Команда» → человек → «🔐 Доступы» включает и выключает разделы: документы, "
        "рабочие файлы, дашборд, риски, выгрузки Excel, недельный отчёт. Без галочки кнопки раздела не видны."
        "</blockquote>",
        "<b>⚠️ Управление рисками</b>",
        "<blockquote expandable>"
        "Бот сам находит риски по каждой открытой партии и по всему импорту: расходы выше обычных у поставщика, "
        "курс ГТД не равен курсу ЦБ, нет цены завода, срок этапа прошёл, партия долго от ГТД, склад без выпуска ИМ40, "
        "нет счёт-фактур, зависимость от одного поставщика или страны, рост расходов поставщика, сбои бота.\n"
        "Балл = вероятность × влияние (1–25): 🔴 15 и выше — сразу сообщение ответственному и Директору; "
        "🟠 8–14 — в реестре и утренней сводке; 🟡 ниже 8 — только в карточке партии.\n"
        "«🏠 Меню» → «⚠️ Риски»: что случилось, что сделать, кто отвечает. Статусы: 🔧 В работе, ✅ Снят "
        "(с комментарием), 🤝 Принят (Директор, с причиной). Директор и Разработчик добавляют риски вручную и меняют "
        "вероятность, влияние и ответственного. Если условие риска ушло, бот сам снимает его."
        "</blockquote>",
        "<b>Роли</b>",
        "<blockquote expandable>"
        + "\n".join(f"{role_label(r)} — {ROLE_INFO[r]}" for r in SELECTABLE) + "\n"
        "Роли назначает только Генеральный Директор. Новый человек нажимает /start — Директору приходит запрос "
        "с кнопками выбора роли. Дать доступ заранее: /adduser ID (ID человек узнаёт командой /id). "
        "Менять роли можно в любой момент: «👥 Команда» или /team.\n"
        "➕ Одному человеку можно дать дополнительную роль (Разработчик, 1С УНФ-Оператор, Специалист по таможне): "
        "он получает уведомления, документы и «Мои партии» за обе роли.\n"
        "Уведомления о каждой смене статуса получают Директор, Разработчик и 1С УНФ-Оператор."
        "</blockquote>",
        "<b>Обозначения</b>",
        "<blockquote expandable>"
        "🟡 — <b>жёлтая ячейка в таблице = сумма не подтверждена</b>. "
        "В таблице расходов такая статья помечена звёздочкой *, себестоимость предварительная\n"
        "❗ — расходы, которые ещё не заполнены (пустые ячейки)\n"
        f"⏰ — работа открыта дольше {cfg.OVERDUE_DAYS} дней\n"
        "⚠️ — цифры в таблице не сходятся (AC ≠ Y+Z+AA+AB или BG ≠ расчёт)"
        "</blockquote>",
        "<b>Названия грузов (столбец A)</b>",
        "<blockquote expandable>"
        "Части одного груза объединяются: «Коко 1 авто (1-2)», «Коко 1 авто (2-2)» и «Коко 1 авто ИМ74» — "
        "один груз «Коко 1 авто». Также не важны avto/авто, заглавные буквы и лишние пробелы. "
        "В поиске такой груз показывается суммой всех частей и по каждой части отдельно."
        "</blockquote>",
        "<b>Статьи расходов → столбцы таблицы</b>",
        "<blockquote expandable>"
        "Цена завода — N ($)\n"
        "Банк (перевод оплаты) — P ($)\n"
        "Таможня: сбор Y, пошлина Z, акциз AA, НДС AB, итого AC (сум)\n"
        "Гигиенический сертификат — AD/AE (сум)\n"
        "Сертификат соответствия — AG/AH (сум), счёт-фактура AI\n"
        "Экология AJ/AK, карантин AM/AN, экспертиза AP (сум)\n"
        "Логистика: наличные AW ($), перечислено AX "
        f"(больше {sh.fmt_num(cfg.LOGISTICS_UZS_THRESHOLD, 0)} — сум, иначе $), перевозчик AY\n"
        "Досмотр BB, инспектор BC ($; в старых строках — сум)\n"
        "Процент завода / агент — BD ($)\n"
        "ПРР BE, прочие BF, AR, AT/AU (сум)\n"
        "Итог всех расходов с товаром — BG ($)\n\n"
        "Сумы переводятся в $ по рыночному курсу S (если пусто — по курсу ГТД H)."
        "</blockquote>",
        "<b>Как пользоваться</b>",
        "<blockquote expandable>"
        "🔔 Новый груз или примечание к ячейке A — бот пришлёт карточку с кнопками: "
        "🔧 В работе · ✅ Завершён · ⚠️ Причина · ⏰ Позже\n"
        f"☀️ Каждый день в {cfg.DAILY_REMINDER_TIME} — сводка открытых работ\n"
        "🔍 Напишите название груза — цена завода и все расходы\n"
        "✅ Завершённые — по месяцам, дата по ГТД ИМ40\n"
        "📊 Итоги — за месяц, год или по открытым работам\n"
        "◀ ▶ — соседний груз, 📄 Подробно — все поля из таблицы\n"
        "💱 Валюты и виды — расходы груза: сколько заплачено в $ и в сумах, по видам расходов "
        "(таможня, логистика, сертификаты…) с долей, наличные / перечисление. "
        "Есть и для всего груза из частей, и в 📊 Итогах за период"
        "</blockquote>",
        "<b>Команды</b>",
        "/gtd номер · /auto номер · /kkdg номер\n"
        "/logist перевозчик · /otpr отправитель\n"
        "/today — сегодня · /cat — категории · /checks — проверки\n"
        "/support — поддержка · /remarks — замечания · /cancel — отменить ввод текста\n"
        "/id — ваш Telegram ID\n"
        "Директору и Разработчику: /reload — перечитать таблицу · /journal — журнал действий · "
        "/status — состояние бота\n"
        "Только Директору: /team — команда и роли · /adduser ID — дать доступ",
    ])


# ===========================================================================
# Грузы, категории и проверки (считаются один раз после каждого чтения таблицы)
# ===========================================================================
CAT_PERIODS = {"a": "всё время", "y": "текущий год"}


def all_cargos() -> list[dict]:
    """Все грузы: части одного названия вместе, с датой по ГТД ИМ40 и ключами категорий. Новые сверху."""
    return app.cached("cargos", lambda: ct.build_cargos(app.records, sh.group_by_base, sh.aggregate))


def cargo_map() -> dict[int, dict]:
    """id строки -> груз, в который она входит."""
    def build():
        return {i: c for c in all_cargos() for i in c["ids"]}
    return app.cached("cargo_map", build)


def cargo_keys(rid: int) -> dict | None:
    c = cargo_map().get(rid)
    return c["keys"] if c else None


def cat_data(per: str) -> tuple[list[dict], dict]:
    """(грузы периода, {раздел: {ключ: Category}}). per: a — всё время, y — текущий год."""
    def build():
        cargos = all_cargos()
        if per == "y":
            year = datetime.now(cfg.TZ).year
            cargos = [c for c in cargos if c["date"] and c["date"].year == year]
        return cargos, ct.index_of(cargos)
    return app.cached(f"cat:{per}", build)


def checks_scan() -> dict[str, list[tuple[dict, str]]]:
    def build():
        res = ck.scan(all_cargos())
        res["cbu"] = cbu_problems()
        return res
    return app.cached("checks", build)


def cargo_status(c: dict) -> str:
    """Статус груза: открытый, если открыта любая его часть."""
    rows = [app.db.get(i) for i in c["ids"]]
    open_rows = [r for r in rows if r and r["status"] in OPEN_STATUSES]
    if open_rows:
        return min(open_rows, key=lambda r: OPEN_STATUSES.index(r["status"]))["status"]
    return DONE if any(r and r["status"] == DONE for r in rows) else ARCHIVE


def is_open_cargo(c: dict) -> bool:
    return cargo_status(c) in OPEN_STATUSES


def cargo_name(c: dict) -> str:
    return sh.display_name(c["title"]) if len(c["parts"]) > 1 else c["title"]


def set_list_state(uid: int, title: str, cargos: list[dict], back: str) -> None:
    """Запоминает список грузов пользователя: карточки листаются ◀ ▶, «☰ К списку» ведёт обратно."""
    groups = [c["ids"] for c in cargos]
    app.searches[uid] = (title, [i for g in groups for i in g], groups)
    app.search_back[uid] = back


def cargo_button(c: dict, gi: int, page: int) -> InlineKeyboardButton:
    d = c["date"].strftime("%d.%m.%y") if c["date"] else "—"
    st = cargo_status(c)
    label = f"{STATUS_ICON[st]} {d} · {short_title(cargo_name(c), 24)}"
    if len(c["parts"]) > 1:
        label += f" · {parts_word(len(c['parts']))}"
    if c["agg"].get("unconfirmed") and st in OPEN_STATUSES:
        label += " · 🟡"
    return btn(label, f"g:{gi}") if len(c["parts"]) > 1 else btn(label, f"c:{c['ids'][0]}:q.{page}")


def pager_fn(make, page: int, pages: int) -> list[InlineKeyboardButton]:
    """Листалка, где callback строится функцией (когда номер страницы не в конце строки)."""
    return [btn("‹ Назад", make(page - 1)) if page > 0 else btn(" ", "noop"),
            btn(f"{page + 1} / {pages}", "noop"),
            btn("Вперёд ›", make(page + 1)) if page < pages - 1 else btn(" ", "noop")]


def fit(text: str) -> str:
    """Не больше одного сообщения Telegram (длина — уже на языке пользователя)."""
    text = i18n.tr(text)
    return text if len(text) <= TELEGRAM_LIMIT else text[:TELEGRAM_LIMIT - 100].rsplit("\n", 1)[0] + "\n…"


def pct_s(v: float | None, decimals: int = 0) -> str:
    return "—" if v is None else sh.fmt_num(v, decimals) + "%"


def cargo_word(n: int) -> str:
    """«1 груз», «3 груза», «7 грузов» (по-русски бот покажет «партия / партии / партий»)."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} груз"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} груза"
    return f"{n} грузов"


def open_cargos_word(n: int) -> str:
    """«у 1 открытого груза», «у 5 открытых грузов»."""
    return f"<b>{n}</b> открытого груза" if n % 10 == 1 and n % 100 != 11 else f"<b>{n}</b> открытых грузов"


def arrow(a: float | None, b: float | None, lower_is_better: bool = True) -> str:
    """▲/▼ — заметно ли значение отличается от «всех грузов» (больше 10%)."""
    if not a or not b or abs(a / b - 1) < 0.10:
        return "≈"
    worse = a > b if lower_is_better else a < b
    return ("🔺" if a > b else "🔻") + (" хуже" if worse else " лучше")


# ===========================================================================
# 🏠 Сегодня — главный экран
# ===========================================================================
def today_view(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    now = int(time.time())
    today = datetime.now(cfg.TZ)
    rows = app.db.open_records()
    counts = {k: len(filtered_open(k)) for k in FILTERS}
    oldest = max(rows, key=days_open) if rows else None
    found = checks_scan()
    issues = sum(len(found[k]) for k in ("bg", "rate", "high", "ac"))
    open_ids = {r["id"] for r in rows}
    no_factory_open = sum(1 for c, _ in found["nofactory"] if open_ids & set(c["ids"]))
    warned7 = app.db.warned_since(now - 7 * 86400)
    admin = is_admin(uid)

    out = [f"🏠 <b>Сегодня · {today.strftime('%d.%m.%Y')}</b>", ""] + health_lines()

    # Главное: что сделать в первую очередь (самое важное сверху, не больше трёх)
    actions: list[tuple[str, str, str]] = []
    if counts["y"]:
        actions.append(("🟡", f"Подтвердить суммы у {open_cargos_word(counts['y'])} — жёлтые ячейки, "
                              "себестоимость пока предварительная", "l:y:0"))
    if counts["o"] and oldest:
        actions.append(("⏰", f"Просрочено <b>{counts['o']}</b> работ — дольше всех «{esc(short_title(oldest['title'], 34))}», "
                              f"{plural_days(days_open(oldest))}", "l:o:0"))
    if no_factory_open:
        actions.append(("🏭", f"Нет цены завода (N) у {open_cargos_word(no_factory_open)}", "x:nofactory:0"))
    if counts["n"]:
        actions.append(("🆕", f"Не взяты в работу: <b>{counts['n']}</b>", "l:n:0"))
    if counts["m"]:
        actions.append(("❗", f"Заполнить расходы у {open_cargos_word(counts['m'])}", "l:m:0"))
    if admin and warned7:
        actions.append(("⚠️", f"Закрыто с пометкой за 7 дней: <b>{len(warned7)}</b>", "j:0"))
    if issues:
        actions.append(("🧪", f"<b>{issues}</b> грузов с вероятной ошибкой в таблице", "x"))
    me = role_of(uid)
    rc = risk_counts()
    if rc["red"]:
        actions.insert(0, ("🔴", f"Высоких рисков: <b>{rc['red']}</b> — разберите в «⚠️ Риски»", "rk:r:0"))
    if counts["d"]:
        my_roles = set(roles_of(uid))
        mine = [r for r in filtered_open("d") if app.resp_map.get(r["id"]) in my_roles & {CUSTOMS, OPERATOR}]
        if mine or admin or can_manage(uid):
            n = len(mine) if mine else counts["d"]
            actions.insert(0, ("⏳", f"Срок этапа прошёл: <b>{n}</b> — напомнить или решить", "l:d:0"))
    if me == CUSTOMS and counts["k"]:
        actions.insert(0, ("🛃", f"Ваши грузы: заполнить расходы — <b>{counts['k']}</b>", "l:k:0"))
    if me == OPERATOR and counts["u"]:
        actions.insert(0, ("🧾", f"Ваши грузы: всё заполнено, завершить — <b>{counts['u']}</b>", "l:u:0"))
    out.append("\n<b>Главное сейчас</b>")
    if actions:
        out += [f"{i}. {icon} {text}" for i, (icon, text, _) in enumerate(actions[:3], 1)]
    else:
        out.append("Всё спокойно 👍 Открытых проблем нет.")

    # Показатели — обычным текстом с иконками (без «копировать»-блока, ровно на любом телефоне)
    closed_today = app.db.closed_count_since(int(today.replace(hour=0, minute=0, second=0).timestamp()))
    closed_7 = app.db.closed_count_since(now - 7 * 86400)
    remarks_n = len(app.db.open_remarks())
    out += ["", "📊 <b>Показатели</b>",
            f"📋 Открытые работы: <b>{len(rows)}</b>",
            f"      🆕 новые {counts['n']} · 🔧 в работе {counts['w']} · ⚠️ с причиной {counts['f']}",
            f"🛃 У таможни: <b>{counts['k']}</b>   🧾 У 1С УНФ: <b>{counts['u']}</b>",
            f"❗ Не заполнено: <b>{counts['m']}</b>   🟡 Не подтверждено: <b>{counts['y']}</b>",
            f"⏰ Просрочено: <b>{counts['o']}</b>" + (f"   ⏳ Срок этапа прошёл: <b>{counts['d']}</b>" if counts["d"] else ""),
            f"📌 Замечаний: <b>{remarks_n}</b>"
            + (f"   🆘 Обращений: <b>{len(app.db.threads_open(200))}</b>" if admin else ""),
            f"✅ Закрыто: сегодня <b>{closed_today}</b> · за 7 дней <b>{closed_7}</b>"
            + (f" · ⚠️ с пометкой {len(warned7)}" if warned7 else "")]

    # Месяц к месяцу (по дате ГТД ИМ40)
    cargos = all_cargos()
    y, m = today.year, today.month
    py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
    cur = [c for c in cargos if c["date"] and (c["date"].year, c["date"].month) == (y, m)]
    prev = [c for c in cargos if c["date"] and (c["date"].year, c["date"].month) == (py, pm)]
    prev_same = [c for c in prev if c["date"].day <= today.day]
    sc, sp = ct.stats(cur), ct.stats(prev)
    ylab = f" {py}" if py != y else ""
    mm = [(f"{MONTHS[m - 1]}, 1–{today.day}", f"{cargo_word(sc['n'])} · {short_usd(sc['landed'])}"),
          (f"{MONTHS[pm - 1]}{ylab}, 1–{today.day}", cargo_word(len(prev_same))),
          (f"{MONTHS[pm - 1]}{ylab} весь", f"{cargo_word(sp['n'])} · {short_usd(sp['landed'])}")]
    out += ["", "📅 <b>Месяц к месяцу</b> <i>(по дате ГТД ИМ40)</i>"]
    out += [f"• {label}: <b>{value}</b>" for label, value in mm]
    if prev_same:
        diff = (sc["n"] - len(prev_same)) / len(prev_same) * 100
        arrow_ = "📈" if diff > 0 else "📉" if diff < 0 else "➖"
        out.append(f"{arrow_} К прошлому месяцу: <b>{diff:+.0f}%</b>".replace("-", "−") + " грузов за те же дни месяца")
    if sc["overhead_pct"] is not None and sp["overhead_pct"] is not None:
        out.append(f"Расходы к заводу: <b>{pct_s(ct.median_pct(cur), 1)}</b> против {pct_s(ct.median_pct(prev), 1)} "
                   "в прошлом месяце <i>(обычные, медиана)</i>")

    kb = [[btn(f"{icon} {i}", cb) for i, (icon, _, cb) in enumerate(actions[:3], 1)]] if actions else []
    kb += [[btn("📋 Открытые", "l:a:0"), btn(f"⏰ {counts['o']}", "l:o:0"), btn(f"🟡 {counts['y']}", "l:y:0"),
            btn(f"❗ {counts['m']}", "l:m:0")],
           [btn(f"🛃 У таможни {counts['k']}", "l:k:0"), btn(f"🧾 У 1С УНФ {counts['u']}", "l:u:0")],
           [btn("🗂 Категории", "k:a"), btn("🧪 Проверки", "x")]]
    if admin:
        kb.append([btn("🗒 Журнал", "j:0"), btn("👤 Сотрудники", "jp:7")])
    kb.append([btn("🖼 Сводка картинкой", "tdi"), btn("🔄 Обновить", "td")])
    return fit("\n".join(out)), InlineKeyboardMarkup(inline_keyboard=kb)


# ===========================================================================
# 🗂 Категории
# ===========================================================================
def per_row(make, per: str) -> list[InlineKeyboardButton]:
    year = datetime.now(cfg.TZ).year
    return [btn(("✓ " if per == "a" else "") + "Всё время", make("a")),
            btn(("✓ " if per == "y" else "") + f"{year} год", make("y"))]


def per_title(per: str) -> str:
    return "всё время" if per == "a" else f"{datetime.now(cfg.TZ).year} год"


def sorted_cats(dim: str, index: dict) -> list:
    cats = list(index[dim].values())
    if dim == "y":   # годы — новые сверху, «без даты» в конце
        return sorted(cats, key=lambda c: ("0" if c.key == "none" else c.key), reverse=True)
    return sorted(cats, key=lambda c: (c.key == "none", -len(c.cargos), c.title.lower()))


def cat_root_view(per: str = "a") -> tuple[str, InlineKeyboardMarkup]:
    cargos, index = cat_data(per)
    s = ct.stats(cargos)
    out = [f"🗂 <b>Категории грузов</b> · {per_title(per)}",
           f"Грузов: <b>{s['n']}</b> · строк таблицы: {s['rows']}",
           f"Себестоимость: <b>{short_usd(s['landed'])}</b> · расходы к заводу обычно {pct_s(ct.median_pct(cargos), 1)}",
           "", "Выберите, как разделить грузы:"]
    for dim in ct.DIM_ORDER:
        icon, name, what = ct.DIMENSIONS[dim]
        top = sorted_cats(dim, index)[:3]
        tops = ", ".join(f"{esc(short_title(c.title, 20))} {len(c.cargos)}" for c in top)
        out.append(f"{icon} <b>{name}</b> · {len(index[dim])} — <i>{tops}</i>")
    kb, row = [], []
    for dim in ct.DIM_ORDER:
        icon, name, _ = ct.DIMENSIONS[dim]
        row.append(btn(f"{icon} {name}", f"kd:{dim}:0:{per}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append(per_row(lambda p: f"k:{p}", per))
    kb.append([btn("🧪 Проверки данных", "x"), btn("🏠 Сегодня", "td")])
    return fit("\n".join(out)), InlineKeyboardMarkup(inline_keyboard=kb)


REGIME_NAMES = {"ИМ40": "Импорт (выпуск для свободного обращения)", "ИМ74": "Таможенный склад",
                "ИМ70": "Временное хранение", "НД40": "Неполная декларация",
                "ИМ51": "Переработка на таможенной территории"}


def regime_split_lines(cargos: list[dict]) -> list[str]:
    """Каждый таможенный режим отдельно: сколько грузов через него прошло и сколько деклараций (строк)."""
    n_cargo: dict[str, int] = {}
    n_rows: dict[str, int] = {}
    for c in cargos:
        seen = set()
        for p in c["parts"]:
            codes = ct.regime_codes(p.get("title", "")) or {"ИМ40"}   # без метки — обычная ИМ40
            for code in codes:
                n_rows[code] = n_rows.get(code, 0) + 1
                seen.add(code)
        for code in seen:
            n_cargo[code] = n_cargo.get(code, 0) + 1
    order = [c for c in REGIME_NAMES if c in n_cargo] + sorted(set(n_cargo) - set(REGIME_NAMES))
    rows = [(f"{code}", f"{cargo_word(n_cargo[code])} · {n_rows[code]} стр.") for code in order]
    out = ["<b>Каждый режим отдельно</b> <i>(груз считается в каждом режиме, через который прошёл)</i>", table(rows)]
    out.append("<blockquote expandable>" + "\n".join(f"<b>{c}</b> — {REGIME_NAMES.get(c, 'другой режим')}" for c in order)
               + "</blockquote>")
    out.append("<b>Сочетания режимов в одном грузе</b>")
    return out


def cat_list_view(dim: str, page: int, per: str) -> tuple[str, InlineKeyboardMarkup]:
    cargos, index = cat_data(per)
    icon, name, what = ct.DIMENSIONS[dim]
    cats = sorted_cats(dim, index)
    total = len(cargos) or 1
    total_landed = ct.stats(cargos)["landed"] or 1
    pages = max(1, (len(cats) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    out = [f"{icon} <b>{name}</b> · {per_title(per)}", f"<i>Разделено по: {what}. Всего: {len(cats)}</i>", ""]
    if dim == "m":
        out += regime_split_lines(cargos) + [""]
    kb = []
    for i, c in enumerate(cats[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page * PAGE_SIZE + 1):
        st = ct.stats(c.cargos)
        share = len(c.cargos) / total
        out.append(f"{i}. <b>{esc(c.title)}</b>\n"
                   f"    <code>{ct.bar(share)}</code> {share:.0%} · грузов: {len(c.cargos)} · {short_usd(st['landed'])}"
                   f" ({st['landed'] / total_landed:.0%} себест.) · расходы {pct_s(ct.median_pct(c.cargos))}")
        kb.append([btn(short_title(c.title, 34), f"kc:{dim}:{c.key}:0:{per}")])   # без чисел: их путают с номером партии
    out.append("\n<i>Доля — от числа грузов; «расходы» — обычные расходы к цене завода (медиана).</i>")
    if pages > 1:
        kb.append(pager_fn(lambda p: f"kd:{dim}:{p}:{per}", page, pages))
    kb.append(per_row(lambda p: f"kd:{dim}:0:{p}", per))
    kb.append([btn("🗂 Все категории", f"k:{per}")])
    return fit("\n".join(out)), InlineKeyboardMarkup(inline_keyboard=kb)


def cat_view(uid: int, dim: str, key: str, page: int, per: str) -> tuple[str, InlineKeyboardMarkup]:
    cargos, index = cat_data(per)
    cat = index.get(dim, {}).get(key)
    icon, name, what = ct.DIMENSIONS.get(dim, ("🗂", "Категория", ""))
    if cat is None:
        text = f"{icon} В разделе «{name}» за {per_title(per)} такой категории нет."
        return text, InlineKeyboardMarkup(inline_keyboard=[per_row(lambda p: f"kc:{dim}:{key}:0:{p}", per),
                                                           [btn(f"{icon} {name}", f"kd:{dim}:0:{per}")]])
    s, al = ct.stats(cat.cargos), ct.stats(cargos)
    med, all_med = ct.median_pct(cat.cargos), ct.median_pct(cargos)
    out = [f"{icon} <b>{esc(cat.title)}</b>", f"<i>{name} · {per_title(per)}</i>", "",
           f"Грузов: <b>{s['n']}</b> из {al['n']} ({s['n'] / (al['n'] or 1):.0%}) · строк таблицы: {s['rows']}"]
    if s["first"]:
        out.append(f"📅 {s['first'].strftime('%m.%Y')} — {s['last'].strftime('%m.%Y')} <i>(по ГТД ИМ40)</i>")
    money: list[tuple[str, str] | None] = [("Цена завода", short_usd(s["factory"])),
                                           ("Расходы", short_usd(s["overhead"])),
                                           ("Себестоимость", short_usd(s["landed"])),
                                           ("  без НДС", short_usd(s["landed_ex_vat"])), None,
                                           ("Расходы к заводу", pct_s(s["overhead_pct"], 1)),
                                           ("  обычно (медиана)", pct_s(med, 1))]
    if s["per_kg"]:
        money.append(("За 1 кг нетто", usd(s["per_kg"])))
    if s["net_kg"]:
        money.append(("Нетто", f"{sh.fmt_num(s['net_kg'] / 1000, 1)} т"))
    out += ["\n💵 <b>Деньги</b>", table(money)]
    if s["with_cost"] < s["n"]:
        out.append(f"<i>Себестоимость посчитана у {s['with_cost']} из {s['n']} грузов (у остальных нет цены завода или курса).</i>")
    share_landed = s["landed"] / (al["landed"] or 1)
    out.append(f"Доля в себестоимости всех грузов: <code>{ct.bar(share_landed)}</code> {share_landed:.0%}")
    if len(index[dim]) > 1:
        out.append(f"\n📊 <b>Сравнение со всеми грузами</b>\n"
                   f"Расходы к заводу: {pct_s(med, 1)} против {pct_s(all_med, 1)} — {arrow(med, all_med)}\n"
                   + (f"За 1 кг: {usd(s['per_kg'])} против {usd(al['per_kg'])} — {arrow(s['per_kg'], al['per_kg'])}"
                      if s["per_kg"] and al["per_kg"] else ""))

    # Из чего состоит категория: годы и главные поставщики / страны
    if dim != "y":
        years = ct.by_year(cat.cargos)[:6]
        if len(years) > 1:
            out += ["\n📅 <b>По годам</b>", table([(y, f"{cargo_word(n)} · {short_usd(v)}") for y, n, v in years])]
    other = "c" if dim == "s" else "s"
    if dim != other:
        cnt: dict[str, list] = {}
        for c in cat.cargos:
            k, label = c["keys"][other]
            cnt.setdefault(k, [label, 0])[1] += 1
        top = sorted(cnt.values(), key=lambda x: -x[1])[:4]
        if top:
            label = "🌍 Страны" if other == "c" else "🏭 Главные поставщики"
            out.append(f"\n{label}: " + ", ".join(f"{esc(short_title(t, 22))} — {n}" for t, n in top))

    opened = [c for c in cat.cargos if is_open_cargo(c)]
    found = checks_scan()
    ids = {i for c in cat.cargos for i in c["ids"]}
    flagged = {id(c) for k in ("bg", "rate", "high", "ac") for c, _ in found[k] if ids & set(c["ids"])}
    marks = []
    if opened:
        marks.append(f"🔧 Открытых работ: <b>{len(opened)}</b>")
    if flagged:
        marks.append(f"🧪 С вероятной ошибкой в таблице: <b>{len(flagged)}</b>")
    if marks:
        out.append("\n" + "\n".join(marks))

    # Список грузов категории (новые сверху)
    pages = max(1, (len(cat.cargos) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    back = f"kc:{dim}:{key}:{page}:{per}"
    set_list_state(uid, cat.title, cat.cargos, back)
    out.append(f"\n<b>Грузы</b> — новые сверху, стр. {page + 1} из {pages}. Нажмите, чтобы открыть карточку.")
    kb = [[cargo_button(c, gi, page)] for gi, c in
          enumerate(cat.cargos[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page * PAGE_SIZE)]
    if pages > 1:
        kb.append(pager_fn(lambda p: f"kc:{dim}:{key}:{p}:{per}", page, pages))
    kb.append([btn("💱 Валюты и виды расходов", f"kv:{dim}:{key}:{per}"), btn("📥 Excel", f"xl:k:{dim}:{key}:{per}")])
    kb.append(per_row(lambda p: f"kc:{dim}:{key}:0:{p}", per))
    kb.append([btn(f"← {name}", f"kd:{dim}:0:{per}"), btn("🗂 Категории", f"k:{per}")])
    return fit("\n".join(out)), InlineKeyboardMarkup(inline_keyboard=kb)


def cat_breakdown_view(dim: str, key: str, per: str) -> tuple[str, InlineKeyboardMarkup]:
    cargos, index = cat_data(per)
    cat = index.get(dim, {}).get(key)
    back = InlineKeyboardMarkup(inline_keyboard=[[btn("← К категории", f"kc:{dim}:{key}:0:{per}")]])
    if cat is None:
        return "Категория не найдена.", back
    parts = [p for c in cat.cargos for p in c["parts"]]
    icon = ct.DIMENSIONS[dim][0]
    return breakdown_html(parts, f"{icon} <b>{esc(cat.title)}</b> · {per_title(per)}\n"
                                 f"Грузов: {len(cat.cargos)} · расходы по валютам и видам"), back


# ===========================================================================
# 🧪 Проверки данных
# ===========================================================================
def checks_root_view() -> tuple[str, InlineKeyboardMarkup]:
    found = checks_scan()
    open_ids = {r["id"] for r in app.db.open_records()}
    out = ["🧪 <b>Проверки данных таблицы</b>",
           "Бот ищет строки, где, скорее всего, ошибка ввода. Таблицу бот не меняет — только показывает, где посмотреть.",
           ""]
    kb = []
    for kind in ck.KIND_ORDER:
        icon, name, _ = ck.KINDS[kind]
        items = found[kind]
        n_open = sum(1 for c, _ in items if open_ids & set(c["ids"]))
        tail = f" · из них открытых: <b>{n_open}</b>" if n_open else ""
        out.append(f"{icon} {name}: <b>{len(items)}</b>{tail}" if items else f"✅ {name}: нет")
        if items:
            kb.append([btn(f"{icon} {short_title(name, 26)} · {len(items)}", f"x:{kind}:0")])
    out.append("\n<b>Что означает</b>")
    out.append("<blockquote expandable>" + "\n".join(f"{ck.KINDS[k][0]} {esc(ck.KINDS[k][2])}" for k in ck.KIND_ORDER)
               + "</blockquote>")
    kb.append([btn("🔄 Обновить", "x"), btn("🏠 Сегодня", "td")])
    return fit("\n".join(out)), InlineKeyboardMarkup(inline_keyboard=kb)


def checks_list_view(uid: int, kind: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    found = checks_scan()
    if kind not in ck.KINDS:
        return checks_root_view()
    icon, name, about = ck.KINDS[kind]
    items = found[kind]
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    out = [f"{icon} <b>{name}</b> · {len(items)}", f"<i>{esc(about)}</i>", ""]
    set_list_state(uid, name, [c for c, _ in items], f"x:{kind}:{page}")
    kb = []
    for gi, (c, label) in enumerate(items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page * PAGE_SIZE):
        d = c["date"].strftime("%d.%m.%Y") if c["date"] else "—"
        rows = sh.rows_label({"_rows": sorted(r for p in c["parts"] for r in p["_rows"])})
        out.append(f"{gi + 1}. {STATUS_ICON[cargo_status(c)]} <b>{esc(cargo_name(c))}</b> · {d}\n"
                   f"    {esc(label)} · строка {rows}")
        kb.append([cargo_button(c, gi, page)])
    if not items:
        out.append("Ничего не найдено 👍")
    if pages > 1:
        kb.append(pager_fn(lambda p: f"x:{kind}:{p}", page, pages))
    kb.append([btn("🧪 Все проверки", "x")])
    return fit("\n".join(out)), InlineKeyboardMarkup(inline_keyboard=kb)


# ===========================================================================
# 🗒 Журнал действий и 👤 сотрудники (Директору и Разработчику)
# ===========================================================================
JOURNAL_PAGE = 12


def journal_view(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    total = app.db.journal_count()
    pages = max(1, (total + JOURNAL_PAGE - 1) // JOURNAL_PAGE)
    page = min(max(page, 0), pages - 1)
    items = app.db.journal(JOURNAL_PAGE, page * JOURNAL_PAGE)
    out = [f"🗒 <b>Журнал действий</b> · записей: {total}", "<i>Кто, что и когда менял. Новые сверху.</i>", ""]
    if not items:
        out.append("Пока никто ничего не менял.")
    for h in items:
        old = h["old"]
        change = (f"{STATUS_ICON.get(old, '•')} → " if old else "") + f"{STATUS_ICON.get(h['status'], '•')} " \
                 f"{STATUS_SHORT.get(h['status'], h['status'])}"
        line = f"<b>{fmt_dt(h['ts'])}</b> · 👤 {esc(h['user_name'] or '—')}\n    {change} · {esc(short_title(h['title'], 40))}"
        if h["status"] == FAIL and h["reason"]:
            line += f"\n    <i>Причина: {esc(short_title(h['reason'], 80))}</i>"
        if h.get("warn"):
            line += f"\n    ⚠️ с пометкой: {esc(h['warn'])}"
        out.append(line)
    kb = []
    if pages > 1:
        kb.append(pager("j", page, pages))
    kb.append([btn("👤 По сотрудникам", "jp:7"), btn("🏠 Сегодня", "td")])
    return fit("\n".join(out)), InlineKeyboardMarkup(inline_keyboard=kb)


def people_view(days: int = 7) -> tuple[str, InlineKeyboardMarkup]:
    stats = app.db.people_stats(int(time.time()) - days * 86400)
    out = [f"👤 <b>Сотрудники · {days} дней</b>",
           "<i>Взял — нажал «В работе», закрыл — «Завершён», ⚠️ — закрыл с пометкой, причина — «Не завершён».</i>", ""]
    if not stats:
        out.append("За этот период действий не было.")
    else:
        lines = [f"{'Кто':<14}{'Взял':>5}{'Закр':>5}{'⚠️':>4}{'Прич':>5}", "─" * 33]
        for p in stats:
            lines.append(f"{short_title(p['name'] or str(p['user_id']), 14):<14}{p['taken'] or 0:>5}"
                         f"{p['closed'] or 0:>5}{p['warned'] or 0:>4}{p['failed'] or 0:>5}")
        out.append("<pre>" + esc("\n".join(lines)) + "</pre>")
    holders: dict[str, int] = {}
    for r in app.db.open_records():
        if r["status"] == WORK:
            holders[r["updated_by"] or "—"] = holders.get(r["updated_by"] or "—", 0) + 1
    if holders:
        out.append("\n🔧 <b>Сейчас в работе у</b>")
        out += [f"• {esc(k)} — {v}" for k, v in sorted(holders.items(), key=lambda x: -x[1])]
    kb = [[btn(("✓ " if d == days else "") + f"{d} дн.", f"jp:{d}") for d in (7, 30, 90)],
          [btn("🗒 Журнал", "j:0"), btn("🏠 Сегодня", "td")]]
    return fit("\n".join(out)), InlineKeyboardMarkup(inline_keyboard=kb)


# ===========================================================================
# Команда и роли (экраны для Генерального Директора)
# ===========================================================================
ROLE_ORDER = {PENDING: 0, ADMIN: 1, DIRECTOR: 1, DEVELOPER: 1, OPERATOR: 2, CUSTOMS: 3, STAFF: 4, VIEWER: 5, DENIED: 6}


def team_members() -> list[dict]:
    """Все, кто связан с ботом: запросившие доступ, выбранные в боте и записанные в файле env."""
    members: dict[int, dict] = {}
    for r in app.db.list_users():
        members[r["user_id"]] = {"id": r["user_id"], "name": r["name"], "username": r["username"],
                                 "db_role": r["role"], "by": r["updated_by"], "at": r["updated_at"],
                                 "requested_at": r["requested_at"]}
    for ids in (cfg.OWNER_IDS, cfg.ALLOWED_USERS, cfg.VIEWER_IDS):
        for uid in ids:
            members.setdefault(uid, {"id": uid, "name": "", "username": "", "db_role": None,
                                     "by": "", "at": 0, "requested_at": 0})
    for m in members.values():
        m["role"] = role_of(m["id"]) or m["db_role"] or DENIED
        m["name"] = m["name"] or app.db.subscriber_name(m["id"])
    return sorted(members.values(), key=lambda m: (ROLE_ORDER.get(m["role"], 9), m["name"].lower(), m["id"]))


def member_title(m: dict) -> str:
    return m["name"] or f"ID {m['id']}"


def team_view(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    members = team_members()
    pages = max(1, (len(members) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    counts = {role: sum(1 for m in members if m["role"] == role) for role in ROLE_ICON}
    out = ["👥 <b>Команда и роли</b>"]
    summary = " · ".join(f"{ROLE_ICON[r]} {counts[r]}" for r in ROLE_ICON if counts[r])
    if summary:
        out.append(summary)
    out.append("")
    out.append("<blockquote expandable>" + "\n".join(f"{role_label(r)} — {ROLE_INFO[r]}" for r in SELECTABLE)
               + "</blockquote>")
    if counts[PENDING]:
        out.append(f"🆕 <b>Ждут решения: {counts[PENDING]}</b>")
    out.append("Нажмите на человека, чтобы выбрать или изменить роль." if members else "Пока никого нет.")
    out.append("➕ Дать доступ человеку, который ещё не писал боту: <code>/adduser 123456789</code> "
               "(его ID он узнаёт командой /id).")
    kb = []
    for m in members[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        extra = "".join(ROLE_ICON[r] for r in roles_of(m["id"])[1:])
        kb.append([btn(f"{ROLE_ICON[m['role']]}{extra} {short_title(member_title(m), 22)} · {m['id']}",
                       f"tu:{m['id']}:{page}")])
    if pages > 1:
        kb.append(pager("tm", page, pages))
    return "\n".join(out), InlineKeyboardMarkup(inline_keyboard=kb)


def team_user_view(uid: int, page: int, viewer_id: int) -> tuple[str, InlineKeyboardMarkup]:
    back = [btn("← К команде", f"tm:{page}")]
    m = next((x for x in team_members() if x["id"] == uid), None)
    if m is None:
        return "Пользователь не найден.", InlineKeyboardMarkup(inline_keyboard=[back])
    role = m["role"]
    name = f"👤 <b>{esc(member_title(m))}</b>" + (f" · @{esc(m['username'])}" if m["username"] else "")
    out = [name, f"🆔 <code>{uid}</code>", "", f"Роль: <b>{role_label(role)}</b>"]
    extras = roles_of(uid)[1:]
    if extras:
        out.append("➕ Дополнительно: <b>" + ", ".join(f"{ROLE_ICON[r]} {ROLE_NAME[r]}" for r in extras) + "</b>")
    if role in ACTIVE_ROLES:
        out.append(f"<i>{ROLE_INFO[role]}</i>")
        acc = access_map(uid)
        out.append("🔐 " + " ".join(f"{'☑️' if acc[k] else '▫️'}{n.split(' ', 1)[0]}" for k, n, _ in ACCESS_KEYS))
    # Администратору из env можно выбрать любую роль, но доступ ему не закрыть.
    # Себе (если вы не из env) — только должность Директор или Разработчик, чтобы не потерять управление.
    owner = uid in cfg.OWNER_IDS
    self_only = uid == viewer_id and not owner
    protected = owner or self_only
    if m["db_role"]:
        if m["db_role"] == PENDING:
            out.append(f"🕓 Запрос от {fmt_dt(m['requested_at'])}")
        elif m["by"] and m["by"] != "запрос":
            out.append(f"Выбрано в боте: {esc(m['by'])}, {fmt_dt(m['at'])}")
    elif uid not in cfg.OWNER_IDS:
        out.append("Роль задана в файле env (ALLOWED_USER_IDS / VIEWER_USER_IDS). Выбранная здесь главнее.")
    if owner:
        out.append("\n🔒 Администратор из файла env (OWNER_IDS): можно выбрать любую роль, закрыть доступ нельзя. "
                   "Пока Генеральный Директор не назначен, он управляет командой при любой роли.")
    elif self_only:
        out.append("\n🔒 Свой полный доступ убрать нельзя, можно выбрать должность — Директор или Разработчик.")
    history = app.db.role_history(uid)
    if history:
        names = {**ROLE_NAME, PENDING: "запрос", DENIED: "закрыт", "": "—"}
        names.update({f"+{r}": f"➕ {ROLE_NAME[r]}" for r in EXTRA_ROLES})
        names.update({f"-{r}": f"➖ {ROLE_NAME[r]}" for r in EXTRA_ROLES})
        out.append("\n<b>История ролей</b>")
        out.append("<blockquote expandable>" + "\n".join(
            f"{fmt_dt(h['ts'])} · {names.get(h['old_role'], h['old_role'])} → {names.get(h['new_role'], h['new_role'])}"
            f" · {esc(h['actor_name'] or '—')}" for h in history) + "</blockquote>")

    kb = []
    for r in ((DIRECTOR, DEVELOPER) if self_only else SELECTABLE):
        kb.append([btn(role_button(r, mark=(role == r)), f"tr:{uid}:{CODE_OF[r]}:{page}")])
    if not protected and role != DENIED:
        kb.append([btn("⛔ Отклонить запрос" if role == PENDING else "⛔ Закрыть доступ", f"tr:{uid}:x:{page}")])
    if role in ACTIVE_ROLES:
        cur_extra = app.db.extra_roles(uid)
        row = []
        for r in EXTRA_ROLES:
            if r == role:
                continue
            on = r in cur_extra
            row.append(btn(("☑️ " if on else "➕ ") + f"{ROLE_ICON[r]} {ROLE_SHORT.get(r, ROLE_NAME[r])}",
                           f"tx:{uid}:{CODE_OF[r]}:{page}"))
        if row:
            kb.append([btn("Дополнительная роль (можно несколько):", "noop")])
            kb.extend([[b] for b in row])
    if role in ACTIVE_ROLES and not (owner or can_manage(uid)):
        over = app.db.access_of(uid)
        kb.append([btn("🔐 Доступы" + (f" · ✍️ {len(over)}" if over else ""), f"ua:{uid}:{page}")])
    kb.append(back)
    return "\n".join(out), InlineKeyboardMarkup(inline_keyboard=kb)


# ===========================================================================
# Отправка
# ===========================================================================
TELEGRAM_LIMIT = 4000


def pack_sections(text: str, limit: int = 3800) -> list[str]:
    """Длинный текст -> несколько сообщений. Режем только между разделами (строка, начинающаяся с <b>),
    чтобы не разорвать <blockquote> и другие теги."""
    sections, cur = [], []
    for line in text.split("\n"):
        if line.startswith("<b>") and cur:
            sections.append("\n".join(cur))
            cur = []
        cur.append(line)
    if cur:
        sections.append("\n".join(cur))
    parts, buf = [], ""
    for sec in sections:
        if buf and len(buf) + len(sec) + 1 > limit:
            parts.append(buf)
            buf = sec
        else:
            buf = f"{buf}\n{sec}" if buf else sec
    if buf:
        parts.append(buf)
    out = []
    for part in parts:                      # раздел сам длиннее лимита — режем по строкам (крайний случай)
        out.extend(split_message(part, limit) if len(part) > limit else [part])
    return out


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


async def send(user_id: int, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    chunks = split_message(i18n.tr(text, user_lang(user_id)))   # длину считаем после перевода
    try:
        for i, chunk in enumerate(chunks):
            await app.bot.send_message(user_id, chunk, reply_markup=kb if i == len(chunks) - 1 else None)
    except TelegramForbiddenError:
        log.info("Пользователь %s заблокировал бота — убран из рассылки", user_id)
        app.db.remove_subscriber(user_id)
    except Exception:
        log.exception("Не удалось отправить сообщение %s", user_id)


def recipients() -> list[int]:
    return [u for u in app.db.subscribers() if is_allowed(u)]


async def send_card(user_id: int, rid: int, header: str = "") -> None:
    token = i18n.CURRENT.set(user_lang(user_id))       # таблицы в карточке — сразу на языке получателя
    try:
        text, kb = card_html(rid, header), card_kb(rid, "-", user_id)
    finally:
        i18n.CURRENT.reset(token)
    await send(user_id, text, kb)


def resp_targets(role: str) -> set[int]:
    """Кому писать про груз: люди с ролью-ответственным; если таких нет — Директор и Разработчик."""
    ids = {u for u in app.db.db_ids_by_roles((role,)) if is_allowed(u)}
    return ids or admin_ids()


async def notify_resp(rids: list[int]) -> None:
    by_role: dict[str, list[int]] = {CUSTOMS: [], OPERATOR: []}
    for rid in rids:
        role = app.resp_map.get(rid, "")
        if role in by_role:
            by_role[role].append(rid)
    for role, ids in by_role.items():
        if not ids:
            continue
        for u in resp_targets(role):
            if len(ids) > 5:
                text = (f"{RESP_ICON[role]} <b>Вам передано грузов: {len(ids)}</b>\n\n"
                        + ("В них снова есть незаполненное." if role == CUSTOMS else "В них всё заполнено — осталось завершить."))
                await send(u, text, InlineKeyboardMarkup(inline_keyboard=[[btn(f"{RESP_ICON[role]} Открыть список",
                                                                           "l:k:0" if role == CUSTOMS else "l:u:0")]]))
            else:
                head = (f"🛃 <b>Груз вернулся к вам: снова есть незаполненное</b>" if role == CUSTOMS
                        else "🧾 <b>Груз передан вам: всё заполнено, осталось завершить</b>")
                for rid in ids:
                    await send_card(u, rid, head)


async def notify_events(events: list[tuple[str, int]]) -> None:
    resp_ids = [rid for kind, rid in events if kind == "resp"]
    events = [e for e in events if e[0] != "resp"]
    if resp_ids:
        await notify_resp(resp_ids)
    users = recipients()
    if not users or not events:
        return
    if len(events) > MAX_EVENT_MESSAGES:
        text = (f"🔔 <b>В таблице {len(events)} новых записей</b>\n\n"
                "Они добавлены в открытые работы. Откройте список, чтобы распределить их.")
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("📋 Открыть список", "l:n:0")]])
        for u in users:
            await send(u, text, kb)
        return
    for kind, rid in events:
        header = {"new": "🔔 <b>Новый груз в таблице</b>", "note": "📝 <b>Новое примечание к грузу</b>",
                  "yellow": "🟡 <b>Груз открыт снова: появилась новая жёлтая ячейка</b>"}.get(kind, "🔔")
        for u in users:
            await send_card(u, rid, header)


async def notify_owners(rid: int, actor_id: int, old_status: str) -> None:
    row = app.db.get(rid)
    text = (f"🔔 <b>Статус изменён</b>\n\n📦 <b>{esc(row['title'])}</b>\n"
            f"{STATUS_ICON[old_status]} {STATUS_NAME[old_status]} → {STATUS_ICON[row['status']]} <b>{STATUS_NAME[row['status']]}</b>\n"
            f"👤 {esc(row['updated_by'])} · {fmt_dt(row['updated_at'])}")
    if row["status"] == FAIL and row["reason"]:
        text += f"\n<blockquote>Причина: {esc(row['reason'])}</blockquote>"
    if row["status"] == DONE and row_warn(row):
        text += f"\n⚠️ <b>Закрыт с пометкой:</b> {esc(row_warn(row))}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("📦 Открыть карточку", f"c:{rid}:-")]])
    for owner in notify_ids() - {actor_id}:
        await send(owner, text, kb)


async def notify_admins(text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    for admin in admin_ids():
        await send(admin, text, kb)


async def notify_sheet_state(action: str, seconds: float) -> None:
    """Тревога «таблица не читается» и сообщение «снова читается» — Директору и Разработчику."""
    if action == "alert":
        hint = ("Возможные причины: файл открыт в Excel (закройте его), переименован или перемещён, "
                "повреждён." if cfg.EXCEL_FILE else
                "Возможные причины: нет интернета, у сервисного аккаунта отняли доступ к таблице, лимит запросов Google.")
        await notify_admins(
            f"🔴 <b>Таблица не читается уже {rel.human_duration(seconds)}</b>\n\n"
            f"<blockquote>{esc(app.watch.last_error or 'причина неизвестна')}</blockquote>\n"
            f"{hint}\n\nБот продолжает пробовать каждые 30 секунд. Данные в карточках — по последнему "
            f"удачному чтению ({fmt_dt(int(app.watch.last_ok)) if app.watch.last_ok else 'его не было'}).")
    elif action == "recovered":
        await notify_admins(f"🟢 <b>Таблица снова читается</b>\n\nПерерыв: {rel.human_duration(seconds)}.")


async def run_backup(send_to_admins: bool = False) -> str | None:
    """Автокопия bot.db. В понедельник (send_to_admins=True) свежая копия уходит файлом в Telegram."""
    try:
        app.db.commit()
        path = rel.make_backup(app.db.db, cfg.BACKUP_DIR, cfg.BACKUP_KEEP)
    except Exception as e:
        log.exception("Не удалось сделать копию базы")
        await notify_admins(f"⚠️ <b>Не удалось сделать копию базы</b>\n<blockquote>{esc(str(e))}</blockquote>")
        return None
    app.db.set_meta("last_backup", str(int(time.time())))
    log.info("Копия базы: %s", path)
    if send_to_admins and cfg.BACKUP_TO_ADMINS:
        caption = f"🗄 Копия базы бота на {datetime.now(cfg.TZ).strftime('%d.%m.%Y %H:%M')}. Храните файл — в нём статусы и история."
        for admin in admin_ids():
            try:
                await app.bot.send_document(admin, FSInputFile(path, filename=os.path.basename(path)),
                                            caption=caption, disable_notification=True)
            except Exception:
                log.warning("Не удалось отправить копию базы пользователю %s", admin)
    return path


async def safe_edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.warning("Не удалось обновить сообщение (%s) — отправляю новое", e)
            await cb.message.answer(text, reply_markup=kb)


# ===========================================================================
# Claude (необязательно): ответы на вопросы обычными словами
# ===========================================================================
SYSTEM_PROMPT = """Ты — помощник отдела закупок импортной компании. Отвечаешь в Telegram по данным учёта импортных грузов.
- Используй ТОЛЬКО данные из <data>. Ничего не выдумывай и не пересчитывай — числа уже посчитаны.
- Сначала коротко ответь на вопрос (1–3 строки), затем при необходимости ключевые цифры.
- Отвечай на языке вопроса (русский или узбекский), по-деловому.
- Формат — Telegram HTML: только <b>, <i>, <code>, <blockquote>. Без Markdown, без <br>, <p>, <ul>, <li>.
- Символы <, > и & в тексте экранируй."""


def card_text(rec: dict) -> str:
    row = app.db.get(rec["id"])
    lines = [f"[Груз «{rec['title']}», строки {sh.rows_label(rec)}, статус: {STATUS_NAME[row['status']]}"
             + (f", причина: {row['reason']}" if row["reason"] else "") + "]"]
    for title, keys in sh.SECTIONS:
        items = [f"  {sh.META[k][0]}: {sh.fmt_value(k, rec[k])}" for k in keys if sh.filled(rec, k)]
        if items:
            lines.append(f"{title}:")
            lines.extend(items)
    lines.append("Не заполнено: " + (", ".join(rec["missing"]) if rec["missing"] else "всё заполнено"))
    return "\n".join(lines)


async def ask_claude(data: str, query: str) -> str:
    client = anthropic.AsyncAnthropic(api_key=cfg.ANTHROPIC_API_KEY)
    response = await client.messages.create(
        model=cfg.CLAUDE_MODEL, max_tokens=cfg.CLAUDE_MAX_TOKENS, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"<data>\n{data}\n</data>\n\nВопрос: {query}"}])
    return "".join(b.text for b in response.content if b.type == "text").strip()


async def show_results(message: Message, found: list[dict], query: str) -> None:
    uid = message.from_user.id
    if not found:
        await message.answer(f"🔍 По запросу «{esc(query)}» ничего не найдено.\n\n"
                             "Попробуйте название груза, номер ГТД, номер машины или ККДГ.")
        return
    groups = sh.group_by_base(found)
    ids = [r["id"] for g in groups for r in g]
    app.searches[uid] = (query, ids, [[r["id"] for r in g] for g in groups])
    app.search_back.pop(uid, None)
    if cfg.ANTHROPIC_API_KEY and len(found) <= cfg.DETAIL_LIMIT:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        try:
            answer = await ask_claude("\n\n".join(card_text(r) for r in found), query)
            if answer:
                try:
                    await message.answer(answer)
                except TelegramBadRequest:
                    await message.answer(html.unescape(re.sub(r"<[^>]+>", "", answer)), parse_mode=None)
        except Exception:
            log.exception("Claude недоступен — показываю карточки")
    if len(found) == 1:
        rid = found[0]["id"]
        await message.answer(card_html(rid), reply_markup=card_kb(rid, "q.0", uid))
        return
    if len(groups) == 1:   # один груз из нескольких частей — сразу «весь груз»
        text, kb = group_view(uid, 0)
        await message.answer(text, reply_markup=kb)
        return
    text, kb = results_view(uid, 0)
    await message.answer(text, reply_markup=kb)


# ===========================================================================
# Доступ и ошибки
# ===========================================================================
def role_of(user_id: int | None) -> str | None:
    """Роль пользователя (код) или None, если доступа нет.
    Порядок: OWNER_IDS из env (всегда полный доступ) → роль, выбранная в боте → строки ALLOWED_USER_IDS / VIEWER_USER_IDS."""
    if user_id is None:
        return None
    row = app.db.get_user(user_id)
    if user_id in cfg.OWNER_IDS:   # администратор из env: любая выбранная должность, доступ не закрывается
        return row["role"] if row and row["role"] in ACTIVE_ROLES else ADMIN
    if row and row["role"] in ACTIVE_ROLES:
        return row["role"]
    if row and row["role"] == DENIED:
        return None
    if user_id in cfg.ALLOWED_USERS:
        return STAFF
    if user_id in cfg.VIEWER_IDS:
        return VIEWER
    return None


EXTRA_ROLES = (DEVELOPER, OPERATOR, CUSTOMS)   # какие роли можно дать человеку дополнительно к основной


def roles_of(user_id: int | None) -> list[str]:
    """Все роли человека: основная и дополнительные. Пусто — доступа нет."""
    main = role_of(user_id)
    if main is None:
        return []
    return [main] + [r for r in app.db.extra_roles(user_id) if r in EXTRA_ROLES and r != main]


def has_role(user_id: int | None, role: str) -> bool:
    return role in roles_of(user_id)


def roles_label(user_id: int | None) -> str:
    """«💻 Разработчик + 🧾 1С УНФ-Оператор»."""
    rs = roles_of(user_id)
    if not rs:
        return ""
    return role_label(rs[0]) + "".join(f" + {ROLE_ICON[r]} {ROLE_SHORT.get(r, ROLE_NAME[r])}" for r in rs[1:])


def is_allowed(user_id: int | None) -> bool:
    return role_of(user_id) is not None


def is_admin(user_id: int | None) -> bool:
    return any(r in ADMIN_ROLES for r in roles_of(user_id))


def can_edit(user_id: int | None) -> bool:
    return any(r in EDIT_ROLES for r in roles_of(user_id))


def admin_ids() -> set[int]:
    """Технический доступ: администраторы из env, Генеральный Директор и Разработчик
    (тревоги, копии базы, журнал, /status, /reload)."""
    return set(cfg.OWNER_IDS) | app.db.db_admin_ids()


def manager_ids() -> set[int]:
    """Кто назначает роли: Генеральный Директор. Пока Директор в боте не назначен — администраторы
    (страховка, чтобы можно было назначить самого Директора)."""
    directors = app.db.db_ids_by_roles((DIRECTOR,))
    return directors or admin_ids()


def can_manage(user_id: int | None) -> bool:
    return user_id is not None and user_id in manager_ids()


def notify_ids() -> set[int]:
    """Кто получает уведомление о каждой смене статуса: Директор, Разработчик, администраторы и 1С УНФ-Оператор."""
    return admin_ids() | app.db.db_ids_by_roles((OPERATOR,))


async def notify_access_request(user) -> None:
    admins = manager_ids()
    if not admins or app.db.count_users(PENDING) > MAX_REQUEST_NOTICES:
        return
    uname = f" · @{esc(user.username)}" if user.username else ""
    text = (f"🔔 <b>Запрос доступа к боту</b>\n\n👤 <b>{esc(user.full_name or '—')}</b>{uname}\n"
            f"🆔 <code>{user.id}</code>\n\nВыберите роль:")
    rows = [[btn(role_button(r), f"tr:{user.id}:{CODE_OF[r]}:0")] for r in SELECTABLE]
    rows.append([btn("⛔ Отклонить", f"tr:{user.id}:x:0"), btn("👥 Команда", "tm:0")])
    for admin in admins:
        await send(admin, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def notify_role_change(user_id: int, role: str) -> None:
    """Сообщение человеку о решении по его доступу."""
    if role in ACTIVE_ROLES:
        await send(user_id, f"✅ <b>Доступ открыт</b>\n\n🔑 Ваша роль: <b>{role_label(role)}</b> — {ROLE_INFO[role]}.\n\n"
                            "Пользуйтесь меню внизу экрана 👇", main_menu(user_id))
    else:
        await send(user_id, "⛔ <b>Доступ закрыт</b>\n\nГенеральный Директор закрыл вам доступ к боту.",
                   ReplyKeyboardRemove())


async def deny(message: Message) -> None:
    user = message.from_user
    row = app.db.get_user(user.id)
    if row is None:   # первый раз — запрос доступа уходит Генеральному Директору
        app.db.add_user_request(user.id, user.full_name or "", user.username or "")
        await notify_access_request(user)
    if row is not None and row["role"] == DENIED:
        tail = "Доступ закрыт. Если это ошибка — свяжитесь с Генеральным Директором."
    elif manager_ids():
        tail = "Запрос отправлен. Когда вам выберут роль, придёт сообщение."
    else:
        tail = "Передайте этот ID администратору бота."
    await message.answer(f"⛔ <b>Доступ закрыт</b>\n\nВаш Telegram-ID: <code>{user.id}</code>\n{tail}")


async def guarded(message: Message, coro_factory) -> None:
    if not is_allowed(message.from_user.id):
        await deny(message)
        return
    try:
        await coro_factory()
    except gspread.exceptions.APIError as e:
        log.exception("Ошибка Google Sheets")
        await message.answer(f"⚠️ Не удалось прочитать таблицу: {esc(str(e))}")
    except (PermissionError, OSError) as e:
        log.exception("Ошибка чтения файла")
        await message.answer(f"⚠️ Не удалось прочитать файл таблицы: {esc(str(e))}")
    except Exception:
        log.exception("Непредвиденная ошибка")
        await message.answer("⚠️ Произошла ошибка, подробности в окне бота.")


# ===========================================================================
# Команды и меню
# ===========================================================================
dp = Dispatcher()

FIELD_COMMANDS: dict[str, tuple[list[str], str]] = {
    "gtd":    (["gtd", "gtd_1", "gtd_2", "gtd_3"], "номер ГТД"),
    "auto":   (["vehicle"], "номер машины"),
    "kkdg":   (["kkdg"], "номер ККДГ"),
    "logist": (["logistics_company"], "название перевозчика"),
    "otpr":   (["sender"], "название отправителя"),
}


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        await deny(message)
        return
    app.db.add_subscriber(message.from_user.id, message.from_user.full_name)
    app.pending_input.pop(message.from_user.id, None)
    role = role_of(message.from_user.id)
    name = esc(message.from_user.first_name or "")
    uid = message.from_user.id
    png = brand.welcome_png(message.from_user.first_name or "", ROLE_NAME[role], user_lang(uid))
    if png:
        await message.answer_photo(BufferedInputFile(png, "uyda.png"))
    if not app.db.get_meta(f"lang_asked:{uid}"):         # первый вход — предложить язык
        app.db.set_meta(f"lang_asked:{uid}", "1")
        await message.answer(LANG_PROMPT, reply_markup=lang_kb(user_lang(uid)))
    await message.answer(
        f"👋 Здравствуйте, {name}!\n\n"
        "Я слежу за таблицей импортных грузов <b>GOLD DISHES / UYDA</b>: напоминаю о новых грузах "
        "и открытых работах, считаю расходы и себестоимость.\n\n"
        f"✅ Вы подписаны на напоминания. Сводка — каждый день в {cfg.DAILY_REMINDER_TIME}.\n"
        f"🔑 Ваша роль: <b>{roles_label(uid)}</b> — {ROLE_INFO[role]}.\n\n"
        "Пользуйтесь меню внизу экрана 👇",
        reply_markup=main_menu(message.from_user.id))


@dp.message(Command("help"))
@dp.message(F.text.in_({MENU_HELP, "❓ Помощь"}))
async def cmd_help(message: Message) -> None:
    parts = pack_sections(i18n.tr(reference_html()))
    for i, part in enumerate(parts):
        await message.answer(part, reply_markup=main_menu(message.from_user.id) if i == len(parts) - 1 else None)


@dp.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


@dp.message(Command("reload"))
async def cmd_reload(message: Message) -> None:
    async def run():
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Принудительно перечитать таблицу могут Генеральный Директор и Разработчик. "
                                 "Бот и так проверяет таблицу сам каждые "
                                 f"{max(1, cfg.SYNC_INTERVAL_SECONDS // 60)} мин.")
            return
        records = await app.refresh(force=True)
        await message.answer(f"🔄 Таблица обновлена: {len(records)} грузов, "
                             f"открытых работ: {len(app.db.open_records())}.")
    await guarded(message, run)


@dp.message(Command("open"))
@dp.message(F.text == MENU_OPEN)
async def cmd_open(message: Message) -> None:
    async def run():
        await app.refresh()
        text, kb = list_view("a", 0)
        await screen(message, text, kb, "l:a:0")
    await guarded(message, run)


@dp.message(F.text == MENU_OVERDUE)
async def menu_overdue(message: Message) -> None:
    async def run():
        await app.refresh()
        text, kb = list_view("o", 0)
        await screen(message, text, kb, "l:o:0")
    await guarded(message, run)


@dp.message(F.text == MENU_DONE)
@dp.message(Command("done"))
async def menu_done(message: Message) -> None:
    async def run():
        await app.refresh()
        text, kb = done_view(message.from_user.id)
        await screen(message, text, kb, "mn:done")
    await guarded(message, run)


@dp.message(F.text == MENU_SEARCH)
async def menu_search(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        await deny(message)
        return
    await message.answer("🔍 Напишите <b>название груза</b> из столбца A — покажу цену завода и все расходы.\n\n"
                         "Можно и номер ГТД, номер машины, ККДГ или перевозчика.",
                         reply_markup=ForceReply(input_field_placeholder="Например: 405 авто"))


@dp.message(Command("itogo"))
@dp.message(F.text == MENU_TOTALS)
async def cmd_totals(message: Message, command: CommandObject | None = None) -> None:
    async def run():
        await app.refresh()
        if command and command.args:
            found = sh.search_cargo(app.records, command.args) or sh.search_free(app.records, command.args)
            await message.answer(totals_html(found, f"«{command.args}»"))
            return
        await screen(message, "📊 <b>Итоги</b>\n\nВыберите период:", period_kb(), "mn:tot")
    await guarded(message, run)


@dp.message(F.text.regexp(r"^/w(\d+)(@\w+)?$"))
async def cmd_card(message: Message) -> None:
    async def run():
        rid = int(re.match(r"^/w(\d+)", message.text).group(1))
        await app.refresh()
        if not app.db.get(rid):
            await message.answer("Груз с таким номером не найден.")
            return
        await message.answer(card_html(rid), reply_markup=card_kb(rid, "-", message.from_user.id))
    await guarded(message, run)


@dp.message(Command(*FIELD_COMMANDS.keys()))
async def cmd_field_search(message: Message, command: CommandObject) -> None:
    fields, what = FIELD_COMMANDS[command.command.lower()]
    if not command.args:
        await message.answer(f"Укажите {what}: <code>/{command.command} значение</code>")
        return

    async def run():
        records = await app.refresh()
        await show_results(message, sh.find_by_fields(records, fields, command.args), command.args)
    await guarded(message, run)


@dp.message(Command("status"))
async def cmd_status(message: Message) -> None:
    async def run():
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Состояние бота доступно только Генеральному Директору и Разработчику.")
            return
        await message.answer(status_html())
    await guarded(message, run)


@dp.message(Command("team"))
@dp.message(F.text == MENU_TEAM)
async def cmd_team(message: Message) -> None:
    async def run():
        if not can_manage(message.from_user.id):
            await message.answer("⛔ Раздел «Команда» доступен только Генеральному Директору.")
            return
        text, kb = team_view(0)
        await screen(message, text, kb, "tm:0")
    await guarded(message, run)


@dp.message(Command("adduser"))
async def cmd_adduser(message: Message, command: CommandObject) -> None:
    """Директор даёт доступ человеку по Telegram-ID, не дожидаясь его запроса: /adduser 123456789."""
    async def run():
        actor = message.from_user.id
        if not can_manage(actor):
            await message.answer("⛔ Давать доступ может только Генеральный Директор.")
            return
        raw = (command.args or "").strip().split()[0] if command.args else ""
        if not raw.isdigit() or not 5 <= len(raw) <= 15:
            await message.answer("Напишите команду и Telegram-ID человека, например: <code>/adduser 123456789</code>\n"
                                 "ID человек узнаёт сам — командой /id в этом боте.")
            return
        uid = int(raw)
        if uid == actor:
            await message.answer("Это ваш собственный ID — свою роль выберите в разделе «👥 Команда».")
            return
        if app.db.get_user(uid) is None and uid not in cfg.OWNER_IDS | cfg.ALLOWED_USERS | cfg.VIEWER_IDS:
            app.db.add_user_request(uid, "", "")   # появится в списке «Команда» как ожидающий роли
        text, kb = team_user_view(uid, 0, actor)
        await message.answer(text + "\n\n<i>Человеку придёт сообщение, когда вы выберете роль. "
                                    "Если он ещё не открывал бота, пусть нажмёт /start.</i>", reply_markup=kb)
    await guarded(message, run)


@dp.message(F.text == MENU_TODAY)
async def menu_today(message: Message) -> None:
    await cmd_today(message)


@dp.message(F.text == MENU_CATS)
async def menu_cats(message: Message) -> None:
    await cmd_cats(message)


@dp.message(F.text == MENU_CHECKS)
async def menu_checks(message: Message) -> None:
    await cmd_checks(message)


# ===========================================================================
# 🏦 Курс ЦБ · 🧾 Для 1С · ⏳ Сроки этапов · 📈 Недельный отчёт · 📥 Excel
# ===========================================================================
def gtd_day(rec: dict | None) -> str | None:
    d = rec.get("gtd_date") if rec else None
    return d.date().isoformat() if hasattr(d, "date") else (d.isoformat() if hasattr(d, "isoformat") else None)


def cbu_check(rec: dict | None) -> dict | None:
    """Сверка курсов строки с курсом ЦБ на дату ГТД (из кэша bot.db). None — курса ЦБ пока нет."""
    day = gtd_day(rec)
    if not day:
        return None
    row = app.db.cbu_get(day)
    if not row or not row["rate"]:
        return None
    return ops.rate_check(num(rec, "usd_rate_gtd"), num(rec, "market_usd_rate"), row["rate"],
                         cfg.CBU_H_TOLERANCE, cfg.CBU_S_TOLERANCE)


def cbu_line(rec: dict | None) -> str:
    chk = cbu_check(rec)
    if not chk:
        return ""
    parts = [f"🏦 Курс ЦБ на дату ГТД: <b>{sh.fmt_num(chk['cbu'], 2)}</b>"]
    if chk["h_dev"] is not None:
        parts.append("курс H ✓" if not chk["h_bad"] else f"⚠️ курс H отличается на <b>{chk['h_dev'] * 100:+.1f}%</b>")
    if chk["s_bad"]:
        parts.append(f"⚠️ курс S отличается на <b>{chk['s_dev'] * 100:+.0f}%</b>")
    return " · ".join(parts)


async def ensure_cbu(days: list[str], limit: int = 6) -> int:
    """Догрузить курсы ЦБ для дат, которых нет в базе (неудачная попытка повторяется через сутки)."""
    if not cfg.CBU_ENABLED:
        return 0
    todo = []
    for day in days:
        row = app.db.cbu_get(day)
        if row is None or (row["rate"] is None and time.time() - row["fetched_at"] > 86400):
            todo.append(day)
        if len(todo) >= limit:
            break
    if not todo:
        return 0
    import aiohttp
    got = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        for day in todo:
            rate = await ops.fetch_cbu_rate(datetime.fromisoformat(day).date(), session)
            app.db.cbu_put(day, rate)
            got += rate is not None
    return got


def cbu_days_wanted() -> list[str]:
    """Даты ГТД: сначала открытые работы, потом остальные — новые сверху."""
    open_ids = {r["id"] for r in app.db.open_records()}
    first, rest = [], []
    for rec in reversed(app.records):
        day = gtd_day(rec)
        if day:
            (first if rec.get("id") in open_ids else rest).append(day)
    return list(dict.fromkeys(first + rest))


def cbu_problems() -> list[tuple[dict, str]]:
    """Для проверки «🏦 Курс ≠ ЦБ»: (груз, подпись)."""
    out = []
    for c in all_cargos():
        labels = []
        for p in c["parts"]:
            chk = cbu_check(p)
            if chk and chk["h_bad"]:
                labels.append(f"H {sh.fmt_num(num(p, 'usd_rate_gtd'), 2)} · ЦБ {sh.fmt_num(chk['cbu'], 2)} "
                              f"({chk['h_dev'] * 100:+.1f}%)")
        if labels:
            out.append((c, labels[0]))
    return out


ck.KINDS["cbu"] = ("🏦", "Курс ГТД ≠ курс ЦБ",
                   "Курс H (на дату ГТД) отличается от официального курса ЦБ Узбекистана на эту дату больше чем на "
                   f"{cfg.CBU_H_TOLERANCE:.0%}. Курс берётся с cbu.uz. Обычно это опечатка в столбце H.")
if "cbu" not in ck.KIND_ORDER:
    ck.KIND_ORDER.insert(2, "cbu")


def one_c_data(rid: int) -> tuple[list, list, str] | None:
    """Реквизиты поступления и доп. расходы строки для 1С УНФ."""
    row, rec = app.db.get(rid), app.by_id.get(rid)
    if rec is None:
        return None
    cats = cargo_keys(rid) or {}
    rate = num(rec, "market_usd_rate") or num(rec, "usd_rate_gtd")
    chk = cbu_check(rec)
    head = [
        ("Груз (столбец A)", row["title"]),
        ("Поставщик (контрагент)", rec["sender"] or ""),
        ("Получатель", rec.get("receiver") or ""),
        ("Номер ГТД", rec["gtd"] or ""),
        ("Дата ГТД", rec["gtd_date"].strftime("%d.%m.%Y") if hasattr(rec.get("gtd_date"), "strftime") else ""),
        ("Таможенный пост", cats.get("p", ("", ""))[1].replace("Пост ", "")),
        ("Таможенный режим", cats.get("m", ("", ""))[1]),
        ("Страна отправления", cats.get("c", ("", ""))[1].split(" ", 1)[-1]),
        ("Условия поставки", rec.get("incoterms") or ""),
        ("Номер машины", rec["vehicle"] or ""),
        ("Цена завода, USD", num(rec, "factory_usd")),
        ("Цена завода в валюте контракта", num(rec, "factory_contract")),
        ("Рыночный курс S", num(rec, "market_usd_rate")),
        ("Курс ГТД H", num(rec, "usd_rate_gtd")),
        ("Курс ЦБ на дату ГТД", chk["cbu"] if chk else None),
        ("Количество мест", num(rec, "places")),
        ("Вес нетто, кг", num(rec, "net_kg")),
        ("Вес брутто, кг", num(rec, "gross_kg")),
        ("Перевозчик", rec["logistics_company"] or ""),
        ("Расходы итого, USD", rec.get("calc_overhead_usd")),
        ("Себестоимость, USD", rec.get("calc_landed_usd")),
        ("Строка таблицы", sh.rows_label(rec)),
    ]
    invoice = {"hyg_cash_uzs": "hyg_invoice", "hyg_transfer_uzs": "hyg_invoice", "cert_cash_uzs": "cert_invoice",
               "cert_transfer_uzs": "cert_invoice", "eco_cash_uzs": "eco_invoice", "eco_transfer_uzs": "eco_invoice",
               "quar_cash_uzs": "quar_invoice", "quar_transfer_uzs": "quar_invoice", "expert_uzs": "expert_invoice",
               "ar_uzs": "ar_invoice", "au_transfer_uzs": "au_invoice", "logistics_transfer": "logistics_invoice",
               "logistics_cash_usd": "logistics_invoice"}
    pay_name = {bd.CASH: "Наличные", bd.TRANSFER: "Перечисление", bd.UNKNOWN: "—"}
    unconf = set(rec.get("unconfirmed", []))
    costs = []
    for kind, cur, pay, value, key in bd.items(rec):
        usd_v = value if cur == bd.USD else (value / rate if rate else None)
        uzs_v = value if cur == bd.UZS else (value * rate if rate else None)
        who = rec["logistics_company"] if kind == "logistics" else ""
        inv = str(rec.get(invoice.get(key, "")) or "").strip()
        party = " · ".join(x for x in (who, f"с/ф {inv}" if inv else "") if x)
        label = re.sub(r"\s*\(исходное значение AX\)", "", sh.META[key][0].split(",")[0])
        costs.append([label, round(value, 2), "USD" if cur == bd.USD else "UZS",
                      round(usd_v, 2) if usd_v is not None else None, round(uzs_v, 2) if uzs_v is not None else None,
                      pay_name[pay], party, "не подтверждено (жёлтая)" if key in unconf else "подтверждено"])
    note = ("Себестоимость предварительная: есть незаполненные или неподтверждённые суммы."
            if [m for m in rec["missing"] if "Счёт-фактура" not in m] or unconf else "")
    return head, costs, note


COST_HEADERS = ["Статья", "Сумма", "Валюта", "Сумма, USD", "Сумма, UZS", "Оплата", "Контрагент / счёт-фактура",
                "Подтверждение"]


def one_c_html(rid: int) -> str:
    data = one_c_data(rid)
    if not data:
        return "ℹ️ Этой записи больше нет в таблице."
    head, costs, note = data
    pick = dict(head)
    out = [f"🧾 <b>Для 1С УНФ</b> · {esc(app.db.get(rid)['title'])}",
           "<i>Нажмите на значение — оно скопируется.</i>", ""]
    for k in ("Поставщик (контрагент)", "Номер ГТД", "Дата ГТД", "Цена завода, USD", "Рыночный курс S",
              "Курс ГТД H", "Курс ЦБ на дату ГТД", "Перевозчик"):
        v = pick.get(k)
        if v not in (None, ""):
            v = sh.fmt_num(v, 2) if isinstance(v, float) else v
            out.append(f"{esc(k)}: <code>{esc(str(v))}</code>")
    if costs:
        out.append("\n<b>Дополнительные расходы</b>")
        total = 0.0
        for c in costs:
            amount = sh.fmt_num(c[1], 2)
            usd_part = f" ≈ {usd(c[3])}" if c[2] == "UZS" and c[3] is not None else ""
            mark = " 🟡" if c[7].startswith("не") else ""
            out.append(f"• {esc(c[0])}: <code>{amount}</code> {c[2]}{usd_part}{mark}")
            total += c[3] or 0
        out.append(f"<b>Итого расходов ≈ {usd(total)}</b>")
    if note:
        out.append(f"\n<i>{esc(note)}</i>")
    out.append("\n📎 Ниже — файл Excel: лист «Поступление» и лист «Доп. расходы».")
    return fit("\n".join(out))


EXPORT_HEADERS = ["Груз", "Статус", "Ответственный", "Дней на этапе", "Режим", "Страна", "Поставщик", "ГТД",
                  "Дата ГТД", "Пост", "Цена завода, $", "Расходы, $", "Себестоимость, $", "Расходы к заводу, %",
                  "Не заполнено", "Не подтверждено", "Строка таблицы"]
EXPORT_MONEY = (11, 12, 13, 14)
EXPORT_WIDTHS = [30, 13, 22, 10, 22, 14, 28, 30, 11, 8, 14, 13, 15, 12, 40, 10, 12]


def _export_row(title, status, rid, rec, cats, agg=None) -> list:
    a = agg or rec or {}
    role = app.resp_map.get(rid, "")
    d = a.get("gtd_date") if a else None
    miss = [m for m in (rec or {}).get("missing", [])]
    return [title, STATUS_NAME.get(status, status), RESP_TITLE.get(role, ""), stage_days(rid) if role else None,
            cats.get("m", ("", ""))[1], cats.get("c", ("", ""))[1].split(" ", 1)[-1],
            (a.get("sender") or ""), (a.get("gtd") or ""), d.strftime("%d.%m.%Y") if hasattr(d, "strftime") else "",
            cats.get("p", ("", ""))[1].replace("Пост ", ""), num(a, "factory_usd"), a.get("calc_overhead_usd"),
            a.get("calc_landed_usd"), a.get("calc_overhead_pct"), ", ".join(miss),
            len((rec or {}).get("unconfirmed", [])), sh.rows_label(rec) if rec else ""]


def export_records(rows) -> list[list]:
    out = []
    for r in rows:
        rec = app.by_id.get(r["id"])
        out.append(_export_row(r["title"], r["status"], r["id"], rec, cargo_keys(r["id"]) or {}))
    return out


def export_cargos(cargos: list[dict]) -> list[list]:
    out = []
    for c in cargos:
        rid = c["ids"][0]
        rec = dict(c["agg"])
        rec["missing"] = sorted({m for i in c["ids"] for m in (app.by_id.get(i) or {}).get("missing", [])})
        rec["unconfirmed"] = [k for i in c["ids"] for k in (app.by_id.get(i) or {}).get("unconfirmed", [])]
        rec["_rows"] = sorted(x for p in c["parts"] for x in p["_rows"])
        out.append(_export_row(cargo_name(c), cargo_status(c), rid, rec, c["keys"], rec))
    return out


def tr_list(items: list[str], lang: str) -> list[str]:
    return [i18n.tr(x, lang) for x in items]


def tr_rows(rows: list[list], lang: str) -> list[list]:
    return [[i18n.tr(v, lang) if isinstance(v, str) else v for v in r] for r in rows]


async def send_xlsx(cb: CallbackQuery, data: bytes, name: str, caption: str) -> None:
    await cb.message.answer_document(BufferedInputFile(data, name), caption=caption)


def safe_name(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")[:40] or "uyda"


def weekly_report(days: int = 7) -> tuple[str, InlineKeyboardMarkup, bytes]:
    now = int(time.time())
    since = now - days * 86400
    today = datetime.now(cfg.TZ)
    start = datetime.fromtimestamp(since, cfg.TZ)
    rows = app.db.open_records()
    done = app.db.done_since(since)
    warned = [r for r in done if row_warn(r)]
    new = [r for r in app.db.db.execute("SELECT * FROM records WHERE opened_at>=? AND status!=?", (since, ARCHIVE))]
    counts = {k: len(filtered_open(k)) for k in FILTERS}
    remarks = app.db.open_remarks()
    threads = app.db.threads_open(200)
    people = app.db.people_stats(since)
    out = [f"📈 <b>Отчёт за неделю</b> · {start.strftime('%d.%m')} – {today.strftime('%d.%m.%Y')}", ""]
    out += [f"🆕 Новых грузов: <b>{len(new)}</b>   ✅ Закрыто: <b>{len(done)}</b>"
            + (f" (⚠️ с пометкой {len(warned)})" if warned else ""),
            "",
            f"📋 Открыто сейчас: <b>{len(rows)}</b>",
            f"🛃 У таможни: <b>{counts['k']}</b>   🧾 У 1С УНФ: <b>{counts['u']}</b>",
            f"⏳ Срок этапа прошёл: <b>{counts['d']}</b>   ⏰ Просрочено: <b>{counts['o']}</b>",
            f"🟡 Не подтверждено: <b>{counts['y']}</b>",
            f"📌 Замечаний: <b>{len(remarks)}</b>   🆘 Обращений: <b>{len(threads)}</b>"]
    late = sorted(filtered_open("d"), key=lambda r: -(stage_days(r["id"]) or 0))[:8]
    if late:
        out.append("\n⏳ <b>Дольше нормы на этапе</b>")
        for r in late:
            role = app.resp_map.get(r["id"], "")
            out.append(f"• {esc(r['title'])} — {RESP_ICON.get(role, '')} {plural_days(stage_days(r['id']) or 0)} "
                       f"(норма {sla_norm(role)})")
    r_new, r_closed = app.db.risks_since(since)
    rc = risk_counts()
    out.append(f"\n⚠️ <b>Риски</b>: сейчас 🔴 {rc['red']} · 🟠 {rc['orange']} · за неделю новых {len(r_new)}, "
               f"снято или принято {len(r_closed)}")
    out += [f"   {rk.level(r['p'] * r['i'])[0]} {r['p'] * r['i']} · {esc(rk.KINDS.get(r['kind'], ('', 'Риск'))[1])} — "
            f"{esc(short_title(r['title'], 30))}" for r in app.db.risks()[:5]]
    if people:
        out.append("\n👤 <b>Кто что сделал</b>")
        out += [f"• {esc(p['name'] or str(p['user_id']))}: взял {p['taken'] or 0}, закрыл {p['closed'] or 0}"
                + (f", ⚠️ {p['warned']}" if p["warned"] else "") for p in people[:8]]
    if done:
        out.append("\n✅ <b>Закрыто за неделю</b>")
        out.append("<blockquote expandable>" + "\n".join(
            f"• {esc(r['title'])} — {esc(r['updated_by'])}" + (" · ⚠️" if row_warn(r) else "") for r in done[:40])
            + "</blockquote>")
    out.append("\n📎 Подробно — в файле Excel.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("📋 Открытые работы", "l:a:0"), btn("⏳ Срок прошёл", "l:d:0")],
                                               [btn("⚠️ Риски", "rk:a:0"), btn("🏠 Сегодня", "td")]])
    people_rows = [[p["name"] or str(p["user_id"]), p["taken"] or 0, p["closed"] or 0, p["warned"] or 0,
                    p["failed"] or 0] for p in people]
    return fit("\n".join(out)), kb, (export_records(rows), export_records(done), people_rows)


def weekly_xlsx(parts, lang: str) -> bytes:
    open_rows, done_rows, people_rows = parts
    H = tr_list(EXPORT_HEADERS, lang)
    return ops.list_xlsx(i18n.tr("Открытые работы", lang), H, tr_rows(open_rows, lang), EXPORT_MONEY,
                         EXPORT_WIDTHS,
                         extra=[(i18n.tr("Закрыто за неделю", lang), H, tr_rows(done_rows, lang)),
                                (i18n.tr("Сотрудники", lang),
                                 tr_list(["Кто", "Взял", "Закрыл", "С пометкой", "Причина"], lang), people_rows)])


async def send_weekly(user_ids: set[int]) -> None:
    text, kb, parts = weekly_report()
    stamp = datetime.now(cfg.TZ).strftime("%Y-%m-%d")
    for u in user_ids:
        await send(u, text, kb)
        try:
            await app.bot.send_document(u, BufferedInputFile(weekly_xlsx(parts, user_lang(u)),
                                                             f"UYDA_week_{stamp}.xlsx"))
        except Exception:
            log.exception("Не удалось отправить файл недельного отчёта %s", u)


async def sla_reminders() -> None:
    """Раз в день: ответственному — грузы, у которых прошёл срок этапа; Директору — где просрочка больше."""
    day = datetime.now(cfg.TZ).date().isoformat()
    late = filtered_open("d")
    by_role: dict[str, list] = {CUSTOMS: [], OPERATOR: []}
    escalate = []
    for r in late:
        role = app.resp_map.get(r["id"], "")
        if role not in by_role:
            continue
        if app.db.sla_mark(r["id"], "remind", day):
            by_role[role].append(r)
        if (stage_days(r["id"]) or 0) > sla_norm(role) + cfg.SLA_ESCALATE_DAYS and app.db.sla_mark(r["id"], "esc", day):
            escalate.append(r)
    for role, items in by_role.items():
        if not items:
            continue
        lines = [f"⏳ <b>Срок этапа прошёл: {len(items)}</b>", "",
                 f"Норма — {sla_norm(role)} дн. Откройте груз и завершите свой этап:"]
        lines += [f"• {esc(r['title'])} — {plural_days(stage_days(r['id']) or 0)}" for r in items[:15]]
        kb = InlineKeyboardMarkup(inline_keyboard=[[item_button(r["id"], "l.d.0")] for r in items[:8]]
                                  + [[btn("⏳ Весь список", "l:d:0")]])
        for u in resp_targets(role):
            await send(u, "\n".join(lines), kb)
    if escalate:
        lines = [f"🚨 <b>Просрочка больше {cfg.SLA_ESCALATE_DAYS} дн. сверх нормы: {len(escalate)}</b>", ""]
        for r in escalate[:15]:
            role = app.resp_map.get(r["id"], "")
            lines.append(f"• {esc(r['title'])} — {RESP_ICON.get(role, '')} {RESP_TITLE.get(role, '')}: "
                         f"{plural_days(stage_days(r['id']) or 0)} (норма {sla_norm(role)})")
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("⏳ Открыть список", "l:d:0")]])
        for u in manager_ids():
            await send(u, "\n".join(lines), kb)
    try:
        await doc_reminders()
    except Exception:
        log.exception("Напоминания о файлах не отправлены")


@dp.callback_query(F.data.startswith("u1:"))
async def cb_one_c(cb: CallbackQuery) -> None:
    rid = await record_cb(cb)
    if rid is None:
        return
    if not can_edit(cb.from_user.id):
        await cb.answer("⛔ Недостаточно прав", show_alert=True)
        return
    rec = app.by_id.get(rid)
    if rec and gtd_day(rec):
        try:
            await ensure_cbu([gtd_day(rec)], 1)
        except Exception:
            pass
    data = one_c_data(rid)
    await cb.message.answer(one_c_html(rid))
    if data:
        lang = user_lang(cb.from_user.id)
        head, costs, note = data
        head = [(i18n.tr(k, lang), i18n.tr(v, lang) if isinstance(v, str) else v) for k, v in head]
        xl = ops.one_c_xlsx(head, tr_rows(costs, lang), tr_list(COST_HEADERS, lang),
                            i18n.tr("Для 1С УНФ", lang) + f" · {app.db.get(rid)['title']}", i18n.tr(note, lang))
        await send_xlsx(cb, xl, f"1C_{safe_name(app.db.get(rid)['title'])}.xlsx", "🧾 <b>Для 1С УНФ</b>")
    await cb.answer()


@dp.callback_query(F.data.startswith("xl:"))
async def cb_excel(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    parts = cb.data.split(":")
    lang = user_lang(cb.from_user.id)
    await app.refresh()
    if parts[1] == "l" and len(parts) >= 3:
        f = parts[2] if parts[2] in FILTERS else "a"
        title = FILTER_TITLES[f]
        rows = export_records(filtered_open(f))
    elif parts[1] == "k" and len(parts) >= 5:
        dim, key, per = parts[2], parts[3], parts[4]
        cat = cat_data(per)[1].get(dim, {}).get(key)
        if cat is None:
            await cb.answer("Категория не найдена.", show_alert=True)
            return
        title = f"{ct.DIMENSIONS[dim][1]}: {cat.title}"
        rows = export_cargos(cat.cargos)
    else:
        await cb.answer()
        return
    xl = ops.list_xlsx(i18n.tr(title, lang)[:31], tr_list(EXPORT_HEADERS, lang), tr_rows(rows, lang),
                       EXPORT_MONEY, EXPORT_WIDTHS)
    stamp = datetime.now(cfg.TZ).strftime("%Y-%m-%d")
    await send_xlsx(cb, xl, f"UYDA_{safe_name(title)}_{stamp}.xlsx", f"📥 <b>{esc(title)}</b> · {len(rows)}")
    await cb.answer()


@dp.callback_query(F.data == "mn:week")
async def cb_weekly(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    if not has_access(cb.from_user.id, "week"):
        await cb.answer("⛔ Недельный отчёт вам не открыт", show_alert=True)
        return
    await app.refresh()
    text, kb, parts = weekly_report()
    await cb.message.answer(text, reply_markup=kb)
    await cb.message.answer_document(BufferedInputFile(weekly_xlsx(parts, user_lang(cb.from_user.id)),
                                                       f"UYDA_week_{datetime.now(cfg.TZ).strftime('%Y-%m-%d')}.xlsx"))
    await cb.answer()


# ===========================================================================
# ⚠️ Управление рисками
# ===========================================================================
rk.COST_PCT, rk.SUPPLIER_SHARE, rk.COUNTRY_SHARE = cfg.RISK_COST_PCT, cfg.RISK_SUPPLIER_SHARE, cfg.RISK_COUNTRY_SHARE
rk.WAREHOUSE_DAYS, rk.AGING_DAYS, rk.ALERT_SCORE = cfg.RISK_WAREHOUSE_DAYS, cfg.RISK_AGING_DAYS, cfg.RISK_ALERT_SCORE
RISK_STATUS = {"open": ("🔓", "Открыт"), "work": ("🔧", "В работе"), "closed": ("✅", "Снят"),
               "accepted": ("🤝", "Принят")}
OWNER_NAME = {"customs": "🛃 Специалист по таможне", "operator": "🧾 1С УНФ-Оператор",
              "director": "👔 Генеральный Директор", "developer": "💻 Разработчик"}


def owner_targets(owner: str) -> set[int]:
    if owner == "customs":
        return resp_targets(CUSTOMS)
    if owner == "operator":
        return resp_targets(OPERATOR)
    if owner == "developer":
        return {u for u in app.db.db_ids_by_roles((DEVELOPER,)) if is_allowed(u)} or admin_ids()
    return manager_ids()


def risk_ctx() -> dict:
    found = checks_scan()
    flags = {k: {i for c, _ in found.get(k, []) for i in c["ids"]} for k in ("rate", "cbu", "bg", "ac")}

    def stage(r):
        role = app.resp_map.get(r, "")
        d = stage_days(r)
        return (d, sla_norm(role), role) if role and d is not None else None
    last_backup = app.db.get_meta("last_backup")
    src, stale = source_info()
    bot_state = {"sheet_down": "Таблица не читается — данные в боте по последнему чтению" if app.watch.is_down else "",
                 "backup_days": int((time.time() - int(last_backup)) // 86400) if last_backup else None,
                 "stale": "Файл таблицы давно не обновлялся — данные могут быть устаревшими" if stale else ""}
    return {"medians": rk.supplier_medians(all_cargos()), "flags": flags, "stage": stage,
            "resp": lambda r: app.resp_map.get(r, ""), "escalate": cfg.SLA_ESCALATE_DAYS, "bot": bot_state}


def risk_scan() -> tuple[dict[int, "rk.CargoRisk"], list]:
    """(id партии -> её риски, риски портфеля). Пересчёт — после каждого чтения таблицы."""
    def build():
        ctx = risk_ctx()
        by_id: dict[int, rk.CargoRisk] = {}
        cargos = all_cargos()
        for c in cargos:
            if not is_open_cargo(c):
                continue
            cr = rk.CargoRisk(c["ids"], cargo_name(c), rk.cargo_signals(c, ctx))
            if cr.signals:
                cr.signals.sort(key=lambda s_: -s_.score)
                for i in c["ids"]:
                    by_id[i] = cr
        return by_id, rk.portfolio_signals(cargos, ctx)
    return app.cached("risks", build)


def risk_line(rid: int) -> str:
    cr = risk_scan()[0].get(rid)
    if not cr:
        return ""
    icon, name = rk.level(cr.score)
    top = cr.signals[0]
    more = [rk.KINDS[s_.kind][1] for s_ in cr.signals[1:3]]
    tail = f"\n      ещё: {', '.join(more)}" if more else ""
    return f"⚠️ Риск: {icon} <b>{cr.score}</b> — {esc(top.why)}{tail}"


async def risk_sync_and_alert() -> None:
    """Реестр = признаки 🟠 и 🔴. Новые 🔴 — сразу ответственному и Директору."""
    if app.db.get_meta("risk_sync_no") == str(app.sync_no) and app.risk_synced:
        return
    by_id, portfolio = risk_scan()
    signals = {}
    for cr in by_id.values():
        for sg in cr.signals:
            if sg.score >= cfg.RISK_REGISTER_SCORE:
                signals[(sg.kind, sg.ref)] = sg
    for sg in portfolio:
        if sg.score >= cfg.RISK_REGISTER_SCORE:
            signals[(sg.kind, sg.ref)] = sg
    raised, _ = app.db.risk_sync(list(signals.values()))
    app.db.set_meta("risk_sync_no", str(app.sync_no))
    first = not app.risk_synced
    app.risk_synced = True
    if first and app.db.get_meta("risk_initialized") != "1":   # первый запуск — без рассылки по всем старым
        app.db.set_meta("risk_initialized", "1")
        for r in app.db.risks():
            app.db.risk_mark_notified(r["id"])
        return
    for rid in raised:
        r = app.db.risk_get(rid)
        if not r or r["notified"] or r["p"] * r["i"] < cfg.RISK_ALERT_SCORE or r["status"] not in ("open", "work"):
            continue
        app.db.risk_mark_notified(rid)
        text = (f"🔴 <b>Высокий риск</b> · балл {r['p'] * r['i']}\n\n"
                f"{rk.KINDS.get(r['kind'], ('⚠️', 'Риск'))[0]} <b>{esc(rk.KINDS.get(r['kind'], ('', 'Риск'))[1])}</b>\n"
                f"📦 {esc(r['title'])}\n<blockquote>{esc(r['why'])}</blockquote>"
                f"👤 Ответственный: {OWNER_NAME.get(r['owner'], '—')}")
        rows = [[btn("⚠️ Открыть риск", f"rv:{rid}")]]
        if r["ref"].startswith("c:"):
            rows[0].append(btn("📦 Карточка партии", f"c:{r['ref'][2:]}:-"))
        for u in owner_targets(r["owner"]) | manager_ids():
            await send(u, text, InlineKeyboardMarkup(inline_keyboard=rows))


def risk_counts() -> dict[str, int]:
    out = {"red": 0, "orange": 0, "yellow": 0}
    for r in app.db.risks():
        sc = r["p"] * r["i"]
        out["red" if sc >= 15 else "orange" if sc >= 8 else "yellow"] += 1
    return out


def can_risk_edit(uid: int) -> bool:
    return can_manage(uid) or is_admin(uid)


RISK_FILTERS = {"a": "Все открытые", "r": "🔴 Высокие", "m": "Мои", "p": "Портфель: поставщики, страны, бот",
                "h": "Снятые и принятые"}


def risks_view(uid: int, flt: str = "a", page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    rows = app.db.risks(("closed", "accepted") if flt == "h" else ("open", "work"))
    if flt == "h":
        rows = sorted(rows, key=lambda r: -(r["closed_at"] or 0))[:80]
    owner_of = {CUSTOMS: "customs", OPERATOR: "operator", DEVELOPER: "developer", DIRECTOR: "director"}
    mine_owners = {owner_of[r] for r in roles_of(uid) if r in owner_of}
    if flt == "r":
        rows = [r for r in rows if r["p"] * r["i"] >= 15]
    elif flt == "m":
        rows = [r for r in rows if r["owner"] in mine_owners]
    elif flt == "p":
        rows = [r for r in rows if not r["ref"].startswith("c:")]
    cnt = risk_counts()
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(page, 0), pages - 1)
    out = ["⚠️ <b>Управление рисками</b>",
           f"🔴 высоких <b>{cnt['red']}</b> · 🟠 средних <b>{cnt['orange']}</b>",
           f"<i>{RISK_FILTERS.get(flt, '')} · {len(rows)}</i>", ""]
    groups: dict[str, int] = {}
    for r in app.db.risks():
        g = rk.KINDS.get(r["kind"], ("", "", "Другое"))[2]
        groups[g] = groups.get(g, 0) + 1
    if groups and flt == "a":
        out.append("По группам: " + " · ".join(f"{g} {n}" for g, n in sorted(groups.items(), key=lambda x: -x[1])))
        out.append("")
    out.append("Нажмите на риск — что случилось, кто отвечает, что сделать." if rows else "Рисков нет 👍")
    kb = [[btn(("✓ " if k == flt else "") + lbl, f"rk:{k}:0") for k, lbl in
           (("a", "Все"), ("r", "🔴"), ("m", "Мои"), ("p", "Портфель"), ("h", "Архив"))]]
    for r in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        sc = r["p"] * r["i"]
        icon = rk.level(sc)[0] if r["status"] in ("open", "work") else RISK_STATUS[r["status"]][0]
        kind_icon = rk.KINDS.get(r["kind"], ("⚠️",))[0]
        kb.append([btn(f"{icon} {sc} · {kind_icon} {short_title(r['title'] or rk.KINDS.get(r['kind'], ('', 'Риск'))[1], 30)}",
                       f"rv:{r['id']}")])
    if pages > 1:
        kb.append(pager_fn(lambda p_: f"rk:{flt}:{p_}", page, pages))
    tools = [btn("📥 Excel", "xl:r")]
    if can_risk_edit(uid):
        tools.insert(0, btn("➕ Добавить риск", "rn"))
    kb.append(tools)
    return fit("\n".join(out)), InlineKeyboardMarkup(inline_keyboard=kb)


def risk_view(uid: int, rid: int) -> tuple[str, InlineKeyboardMarkup]:
    r = app.db.risk_get(rid)
    if not r:
        return "Риск не найден.", InlineKeyboardMarkup(inline_keyboard=[[btn("⚠️ Все риски", "rk:a:0")]])
    sc = r["p"] * r["i"]
    icon, lvl = rk.level(sc)
    k_icon, k_name, group, _ = rk.KINDS.get(r["kind"], ("⚠️", "Риск", "Другое", ""))
    st_icon, st_name = RISK_STATUS.get(r["status"], ("", r["status"]))
    out = [f"{icon} <b>{esc(k_name)}</b> · {lvl} риск", f"📦 {esc(r['title'])}" if r["title"] else None,
           "", f"Балл: <b>{sc}</b> = вероятность {r['p']} × влияние {r['i']}",
           f"Группа: {esc(group)}",
           f"Статус: {st_icon} <b>{st_name}</b>",
           f"👤 Ответственный: {OWNER_NAME.get(r['owner'], '—')}",
           f"{'🤖 Найден ботом' if r['auto'] else '✍️ Добавлен вручную'} · {fmt_dt(r['created_at'])}"]
    if r["why"]:
        out.append(f"\n<b>Что случилось</b>\n<blockquote>{esc(r['why'])}</blockquote>")
    advice = RISK_ADVICE.get(r["kind"])
    if advice:
        out.append(f"<b>Что сделать</b>\n<blockquote>{esc(advice)}</blockquote>")
    if r["note"]:
        out.append(f"📝 {esc(r['note'])}")
    if r["updated_by"] and r["updated_by"] != "бот":
        out.append(f"<i>Изменено: {esc(r['updated_by'])}, {fmt_dt(r['updated_at'])}</i>")
    kb = []
    if can_edit(uid) and r["status"] in ("open", "work"):
        row = []
        if r["status"] == "open":
            row.append(btn("🔧 В работе", f"rs:{rid}:work"))
        row.append(btn("✅ Снят", f"rs:{rid}:closed"))
        if can_risk_edit(uid):
            row.append(btn("🤝 Принять", f"rs:{rid}:accepted"))
        kb.append(row)
    elif can_risk_edit(uid):
        kb.append([btn("🔓 Открыть снова", f"rs:{rid}:open")])
    if can_risk_edit(uid):
        kb.append([btn(f"Вероятность {r['p']} ▾", f"rp:{rid}:p:0"), btn(f"Влияние {r['i']} ▾", f"rp:{rid}:i:0"),
                   btn("👤 Ответственный", f"ro:{rid}:-")])
    if r["ref"].startswith("c:"):
        kb.append([btn("📦 Карточка партии", f"c:{r['ref'][2:]}:-")])
    kb.append([btn("⚠️ Все риски", "rk:a:0")])
    return fit("\n".join(x for x in out if x is not None)), InlineKeyboardMarkup(inline_keyboard=kb)


RISK_ADVICE = {
    "cost_high": "Проверьте статьи расходов в «💵 Расходы»: нет ли суммы в чужом столбце или сумов в столбце долларов. "
                 "Если всё верно — выясните у логиста или брокера причину роста.",
    "rate_cbu": "Сверьте курс H в таблице с курсом ЦБ на дату ГТД (cbu.uz) и исправьте опечатку.",
    "rate_month": "Проверьте курс S и H в строке: из-за ошибки неверно пересчитаны все расходы в сумах.",
    "nofactory": "Внесите цену завода (столбец N) по инвойсу — без неё себестоимость не считается.",
    "bg": "Сверьте итог BG с суммой цены завода и расходов («💵 Расходы»): одна из сумм внесена неверно.",
    "ac": "Сверьте итог таможни AC с суммой сбора, пошлины, акциза и НДС по ГТД.",
    "yellow": "Подтвердите жёлтые суммы документами и снимите жёлтую заливку в таблице.",
    "docs": "Запросите счёт-фактуры у сертификационного органа или перевозчика и внесите номера в таблицу.",
    "sla": "Завершите свой этап или нажмите «⚠️ Причина» на карточке, чтобы Директор видел, что мешает.",
    "aging": "Выясните, что держит партию, и завершите её или укажите причину.",
    "warehouse": "Проверьте, оформлен ли выпуск ИМ40: хранение под складом ограничено по сроку и стоит денег.",
    "supplier_share": "Оцените запасного поставщика по ключевым позициям, чтобы не зависеть от одного.",
    "country_share": "Оцените поставки из другой страны для части ассортимента.",
    "supplier_trend": "Разберите расходы поставщика за 3 месяца: логистика, таможня, агент — и обсудите условия.",
    "bot_sheet": "Проверьте, открыт ли файл в Excel (закройте его), есть ли интернет и доступ к таблице.",
    "bot_backup": "Проверьте, что бот работает ночью (компьютер не спит) — копия делается в 03:00.",
    "bot_stale": "Положите свежую выгрузку таблицы в папку бота и нажмите /reload.",
}


def risks_excel(lang: str) -> bytes:
    H = ["Уровень", "Балл", "Вероятность", "Влияние", "Риск", "Группа", "К чему относится", "Что случилось",
         "Ответственный", "Статус", "Найден", "Обновлён", "Заметка"]
    rows = []
    for r in app.db.risks(("open", "work", "closed", "accepted")):
        sc = r["p"] * r["i"]
        k = rk.KINDS.get(r["kind"], ("", "Риск", "Другое", ""))
        rows.append([rk.level(sc)[1], sc, r["p"], r["i"], k[1], k[2], r["title"], r["why"],
                     OWNER_NAME.get(r["owner"], "").split(" ", 1)[-1], RISK_STATUS.get(r["status"], ("", ""))[1],
                     fmt_dt(r["created_at"]), fmt_dt(r["updated_at"]), r["note"]])
    return ops.list_xlsx(i18n.tr("Реестр рисков", lang), tr_list(H, lang), tr_rows(rows, lang),
                         widths=[10, 7, 11, 9, 30, 14, 30, 60, 26, 12, 16, 16, 30])


@dp.callback_query(F.data.startswith("rk:"))
async def cb_risks(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    _, flt, page = (cb.data.split(":") + ["a", "0"])[:3]
    await app.refresh()
    await risk_sync_and_alert()
    text, kb = risks_view(cb.from_user.id, flt, int(page) if page.isdigit() else 0)
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("rv:"))
async def cb_risk_view(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    text, kb = risk_view(cb.from_user.id, int(cb.data.split(":")[1]))
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("rs:"))
async def cb_risk_status(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    _, rid_s, status = cb.data.split(":")
    rid, uid = int(rid_s), cb.from_user.id
    if not can_edit(uid) or (status in ("accepted", "open") and not can_risk_edit(uid)):
        await cb.answer("⛔ Недостаточно прав", show_alert=True)
        return
    if status in ("accepted", "closed"):
        ask_input(uid, "rk_note", rid, status)
        what = "почему риск принимаем" if status == "accepted" else "что сделано (коротко)"
        await cb.message.answer(f"✍️ Напишите, {what}. Отмена — /cancel",
                                reply_markup=ForceReply(input_field_placeholder="Комментарий…"))
        await cb.answer()
        return
    app.db.risk_update(rid, name_of(cb.from_user), status=status)
    text, kb = risk_view(uid, rid)
    await safe_edit(cb, text, kb)
    await cb.answer(RISK_STATUS[status][1])


@dp.callback_query(F.data.startswith("rp:"))
async def cb_risk_pi(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    _, rid_s, which, val = cb.data.split(":")
    rid, uid = int(rid_s), cb.from_user.id
    if not can_risk_edit(uid):
        await cb.answer("⛔ Недостаточно прав", show_alert=True)
        return
    if val == "0":
        name = "Вероятность" if which == "p" else "Влияние"
        hint = ("1 — почти невозможно · 3 — может случиться · 5 — почти наверняка" if which == "p"
                else "1 — мелочь · 3 — заметные деньги или сроки · 5 — крупный убыток или остановка")
        r = app.db.risk_get(rid)
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn(("✓ " if r[which] == n else "") + str(n), f"rp:{rid}:{which}:{n}")
                                                    for n in range(1, 6)], [btn("← Назад", f"rv:{rid}")]])
        await safe_edit(cb, f"<b>{name}</b> риска «{esc(r['title'] or '')}»\n<i>{hint}</i>", kb)
        await cb.answer()
        return
    app.db.risk_update(rid, name_of(cb.from_user), **{which: int(val)})
    text, kb = risk_view(uid, rid)
    await safe_edit(cb, text, kb)
    await cb.answer("Сохранено")


@dp.callback_query(F.data.startswith("ro:"))
async def cb_risk_owner(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    _, rid_s, owner = cb.data.split(":")
    rid, uid = int(rid_s), cb.from_user.id
    if not can_risk_edit(uid):
        await cb.answer("⛔ Недостаточно прав", show_alert=True)
        return
    if owner == "-":
        r = app.db.risk_get(rid)
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn(("✓ " if r["owner"] == k else "") + v, f"ro:{rid}:{k}")]
                                                   for k, v in OWNER_NAME.items()] + [[btn("← Назад", f"rv:{rid}")]])
        await safe_edit(cb, "👤 <b>Кто отвечает за риск?</b>", kb)
        await cb.answer()
        return
    app.db.risk_update(rid, name_of(cb.from_user), owner=owner)
    r = app.db.risk_get(rid)
    for u in owner_targets(owner) - {uid}:
        await send(u, f"👤 <b>Вам назначен риск</b>\n\n{esc(rk.KINDS.get(r['kind'], ('', 'Риск'))[1])} · "
                      f"{esc(r['title'])}", InlineKeyboardMarkup(inline_keyboard=[[btn("⚠️ Открыть риск", f"rv:{rid}")]]))
    text, kb = risk_view(uid, rid)
    await safe_edit(cb, text, kb)
    await cb.answer("Сохранено")


@dp.callback_query(F.data == "rn")
async def cb_risk_new(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    if not can_risk_edit(cb.from_user.id):
        await cb.answer("⛔ Риски вручную добавляют Директор и Разработчик", show_alert=True)
        return
    ask_input(cb.from_user.id, "rk_new")
    await cb.message.answer("✍️ Опишите риск одним сообщением.\nНапример: «Поставщик ПОСУДА предупредил о задержке "
                            "отгрузки на 2 недели».\nОтмена — /cancel",
                            reply_markup=ForceReply(input_field_placeholder="Описание риска…"))
    await cb.answer()


@dp.callback_query(F.data == "xl:r")
async def cb_risk_excel(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    await risk_sync_and_alert()
    stamp = datetime.now(cfg.TZ).strftime("%Y-%m-%d")
    await send_xlsx(cb, risks_excel(user_lang(cb.from_user.id)), f"UYDA_risks_{stamp}.xlsx", "⚠️ <b>Реестр рисков</b>")
    await cb.answer()


# ===========================================================================
# 🪟 Один экран вместо ленты сообщений
# ===========================================================================
async def screen(message: Message, text: str, kb=None, key: str | None = None) -> None:
    """Показать раздел меню: новое сообщение, а прежний экран меню и нажатая кнопка удаляются,
    чтобы чат не засорялся. Уведомления и карточки из рассылки не трогаются.
    key — какой экран открыт (с него начинается путь для кнопки «◀ Назад»)."""
    uid = message.from_user.id
    app.nav[uid] = [key] if key else []
    sent = await message.answer(text, reply_markup=kb)
    old = app.screens.get(uid)
    app.screens[uid] = getattr(sent, "message_id", None)
    for mid in (old, message.message_id):
        if mid:
            try:
                await app.bot.delete_message(uid, mid)
            except Exception:
                pass   # старое сообщение (>48 ч) или уже удалено — не страшно


@dp.message(F.text.in_({MENU_MORE, "☰ Ещё"}))
async def menu_more(message: Message) -> None:
    if not is_allowed(message.from_user.id):
        await deny(message)
        return
    text, kb = home_view(message.from_user.id)
    await screen(message, text, kb, "hm")


@dp.callback_query(F.data.startswith("mn:"))
async def cb_more(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    uid, item = cb.from_user.id, cb.data.split(":", 1)[1]
    await app.refresh()
    if item == "done":
        text, kb = done_view(uid)
    elif item == "tot":
        text, kb = "📊 <b>Итоги</b>\n\nВыберите период:", period_kb()
    elif item == "rem":
        text, kb = remarks_view(uid)
    elif item == "sup":
        if not can_edit(uid):
            await cb.answer("⛔ Наблюдатель не пишет в поддержку", show_alert=True)
            return
        text, kb = support_view(uid)
    elif item == "lang":
        text, kb = LANG_PROMPT, lang_kb(user_lang(uid))
    elif item == "help":
        parts = pack_sections(i18n.tr(reference_html()))
        await safe_edit(cb, parts[0], None)
        for part in parts[1:]:
            await cb.message.answer(part)
        await cb.answer()
        return
    else:
        text, kb = home_view(uid)
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("ci:"))
async def cb_card_image(cb: CallbackQuery) -> None:
    rid = await record_cb(cb)
    if rid is None:
        return
    data = card_image_data(rid)
    png = brand.card_png(data, user_lang(cb.from_user.id)) if data else None
    if not png:
        await cb.answer("Нужна библиотека Pillow: py -3.12 -m pip install pillow", show_alert=True)
        return
    await cb.message.answer_photo(BufferedInputFile(png, "uyda-cargo.png"),
                                  caption=f"📦 <b>{esc(app.db.get(rid)['title'])}</b>")
    await cb.answer()


# ===========================================================================
# 📎 Документы партии: обмен файлами через бота
# ===========================================================================
# Специалист по таможне и 1С УНФ-Оператор обмениваются рабочими версиями файлов; когда файл готов —
# его отмечают «✅ Финальный», и он сразу уходит Генеральному Директору. Каждый файл привязан к партии,
# у получателя кнопка «✅ Принято», у партии — вся история и версии. Копии — в папке docs рядом с ботом.
DOC_CODES = {"c": CUSTOMS, "o": OPERATOR, "g": DIRECTOR}      # код в кнопке -> кому
DOC_BITS = {"c": 1, "o": 2, "g": 4}
DOC_FINAL = 8                                                 # бит «финальная версия» в маске получателей
DOC_TO_TITLE = {CUSTOMS: "🛃 Специалисту по таможне", OPERATOR: "🧾 1С УНФ-Оператору", DIRECTOR: "👔 Директору"}
DOC_ROLE_SHORT = {CUSTOMS: "🛃 Таможня", OPERATOR: "🧾 1С УНФ", DIRECTOR: "👔 Директор"}
DOC_STAGE = {"work": "🟡 Рабочая версия", "final": "✅ Финальная версия"}
DOC_FILTERS = {"a": "Все", "f": "✅ Финальные", "w": "🟡 Рабочие", "i": "📥 Мне", "o": "📤 От меня"}
DOC_MAX_DOWNLOAD = 20 * 1024 * 1024   # Telegram даёт боту скачать файл до 20 МБ; больше — хранится только в Telegram
DOC_ZIP_MAX = 45 * 1024 * 1024        # архив, который бот может отправить (лимит Telegram — 50 МБ)
DOC_BATCH_WAIT = 1.5                  # секунд ждём остальные файлы, если прислали несколько сразу
DOC_PAGE = 6                          # файлов на одной странице «Документы партии»
DOC_CART_MAX = 20                     # сколько файлов можно прислать за один раз


def doc_folder() -> str:
    return cfg.DOCS_DIR if os.path.isabs(cfg.DOCS_DIR) else os.path.join(cfg.BASE_DIR, cfg.DOCS_DIR)


def doc_cargo(rid: int) -> tuple[int, list[int], str]:
    """(id партии = первая часть, все id частей, название партии)."""
    c = cargo_map().get(rid)
    if c:
        return c["ids"][0], list(c["ids"]), cargo_name(c)
    row = app.db.get(rid)
    return rid, [rid], (row["title"] if row else f"№{rid}")


def doc_mask_roles(mask: int) -> list[str]:
    roles = [DOC_CODES[k] for k in ("c", "o", "g") if mask & DOC_BITS[k]]
    if mask & DOC_FINAL and DIRECTOR not in roles:   # финальный файл всегда получает Директор
        roles.append(DIRECTOR)
    return roles


def doc_default_mask(uid: int) -> int:
    op, cu = has_role(uid, OPERATOR), has_role(uid, CUSTOMS)
    if op and cu:
        return DOC_BITS["g"]               # обе роли сразу — себе не отправляем, по умолчанию Директору
    return DOC_BITS["c"] if op else DOC_BITS["o"]


def doc_targets(role: str, stage: str = "work") -> set[int]:
    ids = {u for u in app.db.db_ids_by_roles((role,)) if is_allowed(u)}
    if role == DIRECTOR and not ids:          # пока Директор не назначен в боте — администратору из env
        ids = {u for u in cfg.OWNER_IDS if role_of(u) == ADMIN}
    need = "docs_work" if stage == "work" else "docs"
    return {u for u in ids if has_access(u, need)}


def doc_role_short(role: str) -> str:
    return DOC_ROLE_SHORT.get(role) or (f"{ROLE_ICON[role]} {ROLE_SHORT.get(role, ROLE_NAME[role])}" if role in ROLE_NAME else "")


def doc_people(role: str, stage: str = "work") -> str:
    names = [user_name_of(u) for u in sorted(doc_targets(role, stage))]
    if not names:
        return "нет в боте"
    text = ", ".join(esc(n) for n in names[:3]) + (f" и ещё {len(names) - 3}" if len(names) > 3 else "")
    if role == DIRECTOR and not app.db.db_ids_by_roles((DIRECTOR,)):
        text += " (пока Директор не назначен — администратору)"
    return text


def user_name_of(uid: int) -> str:
    row = app.db.get_user(uid)
    return (row["name"] if row and row["name"] else "") or f"ID {uid}"


def fsize(n: int | None) -> str:
    n = int(n or 0)
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} МБ".replace(".", ",")
    return f"{max(1, round(n / 1024))} КБ"


def doc_icon(d) -> str:
    name = (d["name"] or "").lower()
    if d["kind"] == "photo" or name.endswith((".jpg", ".jpeg", ".png", ".heic", ".webp")):
        return "🖼"
    if name.endswith((".xls", ".xlsx", ".xlsm", ".csv")):
        return "📊"
    if name.endswith(".pdf"):
        return "📕"
    if name.endswith((".doc", ".docx")):
        return "📝"
    if name.endswith((".zip", ".rar", ".7z")):
        return "🗜"
    return "📄"


def doc_to_text(to_roles: str) -> str:
    return ", ".join(DOC_ROLE_SHORT.get(r, r) for r in (to_roles or "").split(",") if r) or "—"


def doc_norm_name(name: str) -> str:
    stem = os.path.splitext((name or "").lower())[0]
    return re.sub(r"[\s_\-.()]+", " ", re.sub(r"\(\d+\)$", "", stem)).strip()


def doc_versions(docs) -> dict[int, tuple[int, int]]:
    """Версии файлов партии: одинаковое название (без учёта регистра, пробелов и «(1)») — новая версия.
    -> {id файла: (номер версии, сколько всего версий)}. Фото версий не имеют."""
    groups: dict[str, list[int]] = {}
    for d in sorted(docs, key=lambda x: x["id"]):
        if d["kind"] == "photo":
            continue
        groups.setdefault(doc_norm_name(d["name"]), []).append(d["id"])
    out = {}
    for ids in groups.values():
        for i, did in enumerate(ids, 1):
            out[did] = (i, len(ids))
    return out


def doc_version_label(d) -> str:
    v = doc_versions(app.db.docs_for([d["record_id"]])).get(d["id"])
    if not v or v[1] < 2:
        return ""
    return f" · v{v[0]}/{v[1]}" + (" (последняя)" if v[0] == v[1] else "")


def file_of(message: Message) -> dict | None:
    """Файл из сообщения: документ (Excel, PDF, Word, архив…) или фото."""
    if message.document:
        d = message.document
        return {"kind": "document", "file_id": d.file_id, "file_unique_id": d.file_unique_id,
                "name": d.file_name or "файл", "size": d.file_size or 0, "mime": d.mime_type or ""}
    if message.photo:
        p = message.photo[-1]
        stamp = datetime.now(cfg.TZ).strftime("%Y%m%d_%H%M%S")
        return {"kind": "photo", "file_id": p.file_id, "file_unique_id": p.file_unique_id,
                "name": f"photo_{stamp}.jpg", "size": p.file_size or 0, "mime": "image/jpeg"}
    return None


def doc_header(d) -> str:
    return "✅ <b>Финальный документ по партии</b>" if d["stage"] == "final" else "📎 <b>Документ по партии</b>"


def doc_caption(d, header: str | None = None) -> str:
    out = [header or doc_header(d), f"📦 <b>{esc(d['cargo_title'])}</b>",
           f"{doc_icon(d)} {esc(d['name'])} · {fsize(d['size'])}{doc_version_label(d)}",
           DOC_STAGE.get(d["stage"], ""),
           f"👤 От: {esc(d['from_name'])} · {doc_role_short(d['from_role'])}",
           f"➡️ Кому: {doc_to_text(d['to_roles'])}",
           f"🕘 {fmt_dt(d['created_at'])}"]
    if d["stage"] == "final" and d["final_by"] and d["final_by"] != d["from_name"]:
        out.append(f"✅ Финальным отметил(а): {esc(d['final_by'])}, {fmt_dt(d['final_at'])}")
    if d["note"]:
        out.append(f"💬 {esc(d['note'][:400])}")
    return "\n".join(x for x in out if x)[:1000]


def can_finalize(uid: int, d) -> bool:
    return (d["stage"] != "final" and can_edit(uid) and has_access(uid, "docs_work")
            and (uid == d["from_id"] or has_role(uid, OPERATOR) or has_role(uid, CUSTOMS) or can_manage(uid)))


def doc_kb(d, viewer: int) -> InlineKeyboardMarkup:
    rows = []
    recv = {r["user_id"]: r for r in app.db.doc_recv(d["id"])}
    first = []
    if viewer in recv and not recv[viewer]["acked_at"]:
        first.append(btn("✅ Принято", f"da:{d['id']}"))
    if can_finalize(viewer, d):
        first.append(btn("⭐ Сделать финальным", f"df:{d['id']}"))
    if first:
        rows.append(first)
    rows.append([btn("📎 Все документы партии", f"dc:{d['record_id']}:0"), btn("📦 Партия", f"c:{d['record_id']}:-")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def doc_bytes(d) -> bytes | None:
    """Содержимое файла: копия из папки бота, иначе — скачать из Telegram (до 20 МБ)."""
    if d["path"] and os.path.exists(d["path"]):
        try:
            with open(d["path"], "rb") as fh:
                return fh.read()
        except OSError:
            pass
    if int(d["size"] or 0) > DOC_MAX_DOWNLOAD:
        return None
    try:
        buf = await app.bot.download(d["file_id"])
        return buf.getvalue() if buf else None
    except Exception:
        return None


async def doc_send_file(uid: int, d, header: str | None = None) -> bool:
    """Отправить файл человеку (по file_id из Telegram; если не вышло — копией из папки бота)."""
    caption, kb = doc_caption(d, header), doc_kb(d, uid)
    send_fn = app.bot.send_photo if d["kind"] == "photo" else app.bot.send_document
    try:
        await send_fn(uid, d["file_id"], caption=caption, reply_markup=kb)
        return True
    except TelegramForbiddenError:
        log.info("Пользователь %s заблокировал бота — файл не доставлен", uid)
        return False
    except Exception as e:
        path = d["path"]
        if path and os.path.exists(path):
            try:
                await send_fn(uid, FSInputFile(path, filename=d["name"] or None), caption=caption, reply_markup=kb)
                return True
            except Exception:
                pass
        log.warning("Файл №%s не отправлен %s: %s", d["id"], uid, type(e).__name__)
        return False


async def doc_save_copy(doc_id: int, f: dict, title: str) -> str:
    """Копия файла в папку бота: docs/<партия>/<дата_время>_<имя>. Возвращает путь или ''."""
    if not cfg.DOCS_SAVE or int(f.get("size") or 0) > DOC_MAX_DOWNLOAD:
        return ""
    try:
        folder = os.path.join(doc_folder(), re.sub(r"[^\w\-]+", "_", title, flags=re.UNICODE).strip("_")[:60] or "partiya")
        os.makedirs(folder, exist_ok=True)
        stem, ext = os.path.splitext(f["name"] or "file")
        stem = re.sub(r"[^\w\-.]+", "_", stem, flags=re.UNICODE).strip("_.")[:60] or "file"
        ext = re.sub(r"[^\w.]+", "", ext)[:10]
        stamp = datetime.now(cfg.TZ).strftime("%Y-%m-%d_%H%M")
        path = os.path.join(folder, f"{stamp}_{stem}{ext}")
        n = 2
        while os.path.exists(path):
            path = os.path.join(folder, f"{stamp}_{stem}_{n}{ext}")
            n += 1
        await app.bot.download(f["file_id"], destination=path)
        app.db.doc_set_path(doc_id, path)
        return path
    except Exception as e:
        log.warning("Копия файла №%s не сохранена: %s", doc_id, type(e).__name__)
        return ""


async def doc_send_to_roles(uid: int, doc_id: int, roles: list[str], stage: str,
                            header: str | None = None) -> set[int]:
    """Разослать файл людям с этими ролями (кроме отправителя и тех, кто уже получил). -> кому доставлено."""
    targets: set[int] = set()
    for r in roles:
        targets |= doc_targets(r, stage)
    targets.discard(uid)
    already = {r["user_id"] for r in app.db.doc_recv(doc_id)}
    delivered = set()
    for u in sorted(targets - already):
        app.db.doc_recv_add(doc_id, u, user_name_of(u))
        if await doc_send_file(u, app.db.doc_get(doc_id), header):
            delivered.add(u)
    return delivered


async def doc_deliver(uid: int, f: dict, rid: int, mask: int, batch: int | None) -> tuple[int, set[int]]:
    """Сохранить файл у партии, сделать копию и отправить получателям. -> (id файла, кому доставлено)."""
    key, _, title = doc_cargo(rid)
    stage = "final" if mask & DOC_FINAL else "work"
    roles = doc_mask_roles(mask)
    row = app.db.get_user(uid)
    me = (row["name"] if row and row["name"] else "") or f.get("_from_name") or f"ID {uid}"
    doc_id = app.db.doc_add(key, title, batch, f, uid, me, role_of(uid) or "", ",".join(roles),
                            (f.get("note") or "")[:500], stage)
    await doc_save_copy(doc_id, f, title)
    return doc_id, await doc_send_to_roles(uid, doc_id, roles, stage)


def doc_batch_id() -> int:
    """Номер пачки файлов (отправленных за один раз) — общий «✅ Принято» на всю пачку."""
    return int(time.time() * 1000)


def doc_cart(uid: int) -> dict | None:
    cart = app.doc_cart.get(uid)
    if cart and time.time() - cart["ts"] > INPUT_TTL:
        app.doc_cart.pop(uid, None)
        return None
    return cart


def doc_visible(uid: int, docs) -> list:
    """Без доступа к рабочим файлам человек видит только финальные."""
    if has_access(uid, "docs_work"):
        return list(docs)
    return [d for d in docs if d["stage"] == "final"]


def doc_default_filter(uid: int, docs) -> str:
    if not has_access(uid, "docs_work"):
        return "f"
    if role_of(uid) == DIRECTOR and any(d["stage"] == "final" for d in docs):
        return "f"                 # Директору сразу готовые файлы
    return "a"


def doc_filter(uid: int, docs, recv: dict, flt: str) -> list:
    if flt == "f":
        return [d for d in docs if d["stage"] == "final"]
    if flt == "w":
        return [d for d in docs if d["stage"] != "final"]
    if flt == "i":
        return [d for d in docs if any(r["user_id"] == uid for r in recv.get(d["id"], []))]
    if flt == "o":
        return [d for d in docs if d["from_id"] == uid]
    return list(docs)


def docs_view(uid: int, rid: int, page: int = 0, flt: str = "") -> tuple[str, InlineKeyboardMarkup]:
    """📎 Документы партии: все файлы, этапы и версии, кто прислал, кому, кто принял."""
    key, ids, title = doc_cargo(rid)
    alldocs = doc_visible(uid, app.db.docs_for(ids))
    recv = app.db.doc_recv_map([d["id"] for d in alldocs])
    if flt not in DOC_FILTERS or (flt != "f" and not has_access(uid, "docs_work")):
        flt = doc_default_filter(uid, alldocs)
    docs = doc_filter(uid, alldocs, recv, flt)
    versions = doc_versions(app.db.docs_for(ids))
    finals = sum(1 for d in alldocs if d["stage"] == "final")
    waiting = sum(1 for d in alldocs if any(not r["acked_at"] for r in recv.get(d["id"], [])))
    out = ["📎 <b>Документы партии</b>", f"📦 <b>{esc(title)}</b>", ""]
    if not alldocs:
        out.append("Файлов пока нет.")
    else:
        out.append(f"Файлов: <b>{len(alldocs)}</b> · ✅ финальных: <b>{finals}</b>"
                   + (f" · ждут «Принято»: <b>{waiting}</b>" if waiting else " · все приняты ✅"))
        if flt != "a":
            out.append(f"Фильтр: <b>{DOC_FILTERS[flt]}</b> · {len(docs)}")
    pages = max(1, (len(docs) + DOC_PAGE - 1) // DOC_PAGE)
    page = min(max(page, 0), pages - 1)
    kb = []
    if alldocs and has_access(uid, "docs_work"):
        def fb(code):
            n = len(doc_filter(uid, alldocs, recv, code))
            label = DOC_FILTERS[code] + (f" · {n}" if code in ("a", "f", "w") else "")
            return btn(("• " if code == flt else "") + label, f"dc:{key}:0:{code}")
        kb.append([fb("a"), fb("f"), fb("w")])
        kb.append([fb("i"), fb("o")])
    if alldocs and not docs:
        out.append("\nВ этом фильтре файлов нет.")
    for d in docs[page * DOC_PAGE:(page + 1) * DOC_PAGE]:
        rs = recv.get(d["id"], [])
        if not rs:
            state = "📁 сохранён у партии"
        elif all(r["acked_at"] for r in rs):
            state = "✅ принято: " + ", ".join(esc(r["user_name"] or "") for r in rs)
        else:
            ok = [r for r in rs if r["acked_at"]]
            state = "⏳ ждёт «Принято»: " + ", ".join(esc(r["user_name"] or "") for r in rs if not r["acked_at"])
            if ok:
                state += " · ✅ " + ", ".join(esc(r["user_name"] or "") for r in ok)
        mine = any(r["user_id"] == uid and not r["acked_at"] for r in rs)
        v = versions.get(d["id"])
        vtxt = f" · v{v[0]}" if v and v[1] > 1 else ""
        stage = "✅" if d["stage"] == "final" else "🟡"
        out.append("")
        out.append(f"{stage} {doc_icon(d)} <b>{esc(short_title(d['name'], 48))}</b>{vtxt} · {fsize(d['size'])}"
                   + (" · 🔔 вам" if mine else ""))
        out.append(f"<i>{fmt_dt(d['created_at'])}</i> · {esc(d['from_name'])} → {doc_to_text(d['to_roles'])}")
        if d["note"]:
            out.append(f"💬 {esc(short_title(d['note'], 120))}")
        out.append(state)
        kb.append([btn(f"{stage} {short_title(d['name'], 28)}{vtxt}" + (" 🔔" if mine else ""), f"dg:{d['id']}")])
    if pages > 1:
        kb.append(pager_fn(lambda p: f"dc:{key}:{p}:{flt}", page, pages))
    if can_edit(uid):
        kb.append([btn("📤 Отправить файлы", f"du:{key}:{doc_default_mask(uid)}")])
    if alldocs:
        z = [btn("🗜 Архив: все", f"dy:{key}:a")] if has_access(uid, "docs_work") else []
        if finals:
            z.append(btn("🗜 Архив: финальные", f"dy:{key}:f"))
        if z:
            kb.append(z)
    last = [btn("📦 Карточка партии", f"c:{key}:-")]
    if alldocs:
        last.append(btn("📥 Реестр Excel", f"dx:{key}"))
    kb.append(last)
    return fit("\n".join(out)), InlineKeyboardMarkup(inline_keyboard=kb)


def doc_pick_view(uid: int, rid: int, mask: int) -> tuple[str, InlineKeyboardMarkup]:
    """Кому отправить: галочки получателей и этап файла (рабочий / финальный)."""
    key, _, title = doc_cargo(rid)
    cart = doc_cart(uid)
    loose = len(cart["files"]) if cart and cart.get("files") else 0
    final = bool(mask & DOC_FINAL)
    stage = "final" if final else "work"
    out = ["📤 <b>Отправить файлы</b>", f"📦 <b>{esc(title)}</b>", ""]
    if loose:
        out.append(f"Файлов к отправке: <b>{loose}</b>")
        out += [f"• {esc(short_title(f['name'], 50))}" for f in cart["files"][:8]]
        out.append("")
    out.append("Кому отправить? Отметьте одного или нескольких:")
    kb = []
    for code in ("c", "o", "g"):
        role = DOC_CODES[code]
        forced = final and role == DIRECTOR
        on = bool(mask & DOC_BITS[code]) or forced
        out.append(f"{'☑️' if on else '▫️'} {DOC_TO_TITLE[role]}: <i>{doc_people(role, stage)}</i>")
        label = ("☑️ " if on else "▫️ ") + DOC_TO_TITLE[role] + (" · финальный" if forced else "")
        kb.append([btn(label, "noop" if forced else f"du:{key}:{mask ^ DOC_BITS[code]}")])
    out.append("")
    if final:
        out.append("✅ <b>Финальная версия</b> — файл готов, Директор получит его сразу.")
    else:
        out.append("🟡 <b>Рабочая версия</b> — для работы между Специалистом по таможне и Оператором. "
                   "Директору не уходит, пока его не отметят.")
    kb.append([btn("✅ Финальная версия" if final else "▫️ Финальная версия — сразу Директору",
                   f"du:{key}:{mask ^ DOC_FINAL}")])
    if loose:
        kb.append([btn(f"📤 Отправить · {loose} файл.", f"ds:{key}:{mask}")])
        kb.append([btn("✖ Отменить", "dz")])
    else:
        kb.append([btn("📎 Дальше — прислать файлы", f"dw:{key}:{mask}")])
    return "\n".join(out), InlineKeyboardMarkup(inline_keyboard=kb)


def doc_choose_view(uid: int, found: list[int] | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Файл прислали без партии — к какой партии его отнести."""
    cart = doc_cart(uid) or {"files": []}
    files = cart["files"]
    out = [f"📎 <b>Получено файлов: {len(files)}</b>"]
    out += [f"• {esc(short_title(f['name'], 50))}" for f in files[:8]]
    out += ["", "К какой партии их отнести?"]
    kb = []
    if found is not None:
        cand = found
        if not cand:
            out.append("\n🔍 Ничего не найдено — попробуйте другое название, номер ГТД или машины.")
    else:
        my_roles = set(roles_of(uid)) & {CUSTOMS, OPERATOR}
        opened = [c for c in all_cargos() if is_open_cargo(c)]
        if my_roles:
            mine = [c for c in opened if any(app.resp_map.get(i) in my_roles for i in c["ids"])]
            opened = mine + [c for c in opened if c not in mine]
        recent = app.db.docs_recent_records(4)
        cand = []
        for c in opened:
            if c["ids"][0] not in cand:
                cand.append(c["ids"][0])
        cand = [r for r in recent if r not in cand] + cand
    for r in cand[:8]:
        key, _, title = doc_cargo(r)
        mark = "🛃 " if app.resp_map.get(key) == CUSTOMS else "🧾 " if app.resp_map.get(key) == OPERATOR else "📦 "
        kb.append([btn(mark + short_title(title, 34), f"dp:{key}")])
    kb.append([btn("🔎 Найти партию", "dq"), btn("✖ Отменить", "dz")])
    return "\n".join(out), InlineKeyboardMarkup(inline_keyboard=kb)


def docs_inbox_view(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    """📎 Документы: что ждёт вашего «Принято», готовые (финальные) файлы и последние партии с файлами."""
    unacked = doc_visible(uid, app.db.docs_unacked(uid))
    by_rec: dict[int, list] = {}
    for d in unacked:
        by_rec.setdefault(d["record_id"], []).append(d)
    out = ["📎 <b>Документы</b>", ""]
    kb = []
    if by_rec:
        out.append(f"🔔 Ждут вашего «Принято»: <b>{len(unacked)}</b>")
        for rec_id, ds in list(by_rec.items())[:8]:
            fin = sum(1 for d in ds if d["stage"] == "final")
            out.append(f"• {esc(short_title(ds[0]['cargo_title'], 40))} — {len(ds)} · от {esc(ds[0]['from_name'])}"
                       + (f" · ✅ {fin}" if fin else ""))
            kb.append([btn(f"🔔 {short_title(ds[0]['cargo_title'], 28)} · {len(ds)}", f"dc:{rec_id}:0")])
    else:
        out.append("🔔 Новых файлов для вас нет 👍")
    finals = [r for r in app.db.docs_recent_records(6, final_only=True) if r not in by_rec]
    if finals:
        out += ["", "✅ <b>Готовые (финальные) документы</b>"]
        for r in finals[:5]:
            _, ids, title = doc_cargo(r)
            n = sum(1 for d in app.db.docs_for(ids) if d["stage"] == "final")
            kb.append([btn(f"✅ {short_title(title, 30)} · {n}", f"dc:{r}:0:f")])
    if has_access(uid, "docs_work"):
        recent = [r for r in app.db.docs_recent_records(8) if r not in by_rec and r not in finals[:5]]
        if recent:
            out += ["", "🕘 <b>Последние партии с файлами</b>"]
            for r in recent[:5]:
                _, ids, title = doc_cargo(r)
                total, _ = app.db.doc_counts(ids)
                kb.append([btn(f"📎 {short_title(title, 30)} · {total}", f"dc:{r}:0")])
    if can_edit(uid):
        out += ["", "<i>Как отправить: пришлите файл боту — он спросит, к какой партии и кому. "
                    "Или откройте партию → «📎 Документы» → «📤 Отправить файлы».</i>"]
    if app.db.docs_all():
        kb.append([btn("📥 Реестр всех документов (Excel)", "dx:0")])
    return "\n".join(out), InlineKeyboardMarkup(inline_keyboard=kb)


DOC_XL_HEADERS = ["Дата", "Партия", "Файл", "Версия", "Этап", "Размер", "От кого", "Роль", "Кому", "Комментарий",
                  "Получатели", "Приняли", "Копия в папке бота"]


def docs_xlsx(docs, lang: str) -> bytes:
    recv = app.db.doc_recv_map([d["id"] for d in docs])
    vers: dict[int, tuple[int, int]] = {}
    for rec_id in {d["record_id"] for d in docs}:
        vers.update(doc_versions(app.db.docs_for([rec_id])))
    rows = []
    for d in docs:
        rs = recv.get(d["id"], [])
        v = vers.get(d["id"])
        rows.append([fmt_dt(d["created_at"]), d["cargo_title"], d["name"], f"v{v[0]}" if v and v[1] > 1 else "",
                     DOC_STAGE.get(d["stage"], ""), fsize(d["size"]), d["from_name"],
                     doc_role_short(d["from_role"]), doc_to_text(d["to_roles"]),
                     d["note"], ", ".join(r["user_name"] for r in rs),
                     ", ".join(f"{r['user_name']} {fmt_dt(r['acked_at'])}" for r in rs if r["acked_at"]),
                     os.path.relpath(d["path"], cfg.BASE_DIR) if d["path"] else ""])
    return ops.list_xlsx(i18n.tr("Документы партий", lang), tr_list(DOC_XL_HEADERS, lang), tr_rows(rows, lang),
                         widths=[16, 30, 34, 8, 20, 10, 22, 16, 26, 34, 28, 30, 40])


async def docs_zip(uid: int, rid: int, flt: str) -> tuple[bytes | None, int, int]:
    """Архив файлов партии: папки «Финальные» и «Рабочие». -> (zip, файлов в архиве, пропущено)."""
    import zipfile
    _, ids, _ = doc_cargo(rid)
    docs = doc_visible(uid, app.db.docs_for(ids))
    if flt == "f":
        docs = [d for d in docs if d["stage"] == "final"]
    vers = doc_versions(app.db.docs_for(ids))
    buf, total, added, skipped, used = io.BytesIO(), 0, 0, 0, set()
    lang = user_lang(uid)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in sorted(docs, key=lambda x: x["id"]):
            data = await doc_bytes(d)
            if data is None or total + len(data) > DOC_ZIP_MAX:
                skipped += 1
                continue
            folder = i18n.tr("Финальные" if d["stage"] == "final" else "Рабочие", lang)
            stem, ext = os.path.splitext(d["name"] or "file")
            v = vers.get(d["id"])
            stamp = datetime.fromtimestamp(d["created_at"], cfg.TZ).strftime("%Y-%m-%d_%H%M")
            name = f"{folder}/{stamp}_{stem}" + (f"_v{v[0]}" if v and v[1] > 1 else "") + ext
            while name in used:
                name = name.replace(ext, "_" + str(d["id"]) + ext, 1) if ext else name + f"_{d['id']}"
            used.add(name)
            z.writestr(name, data)
            total += len(data)
            added += 1
    return (buf.getvalue() if added else None), added, skipped


async def doc_summary(uid: int, rid: int, mask: int, sent: list[tuple[dict, set[int]]]) -> None:
    key, _, title = doc_cargo(rid)
    people = set().union(*(s for _, s in sent)) if sent else set()
    final = bool(mask & DOC_FINAL)
    out = [f"✅ <b>Отправлено файлов: {len(sent)}</b>", f"📦 <b>{esc(title)}</b>",
           DOC_STAGE["final" if final else "work"],
           f"➡️ Кому: {doc_to_text(','.join(doc_mask_roles(mask)))}"]
    out += [f"• {esc(short_title(f['name'], 50))}" for f, _ in sent[:10]]
    if people:
        out.append("👤 Получили: " + ", ".join(esc(user_name_of(u)) for u in sorted(people)))
        out.append("<i>Когда получатель нажмёт «✅ Принято», вам придёт сообщение.</i>")
    else:
        stage = "final" if final else "work"
        miss = [DOC_TO_TITLE[r] for r in doc_mask_roles(mask) if not (doc_targets(r, stage) - {uid})]
        out.append("⚠️ Некому доставить" + (": " + ", ".join(miss) + " — нет в боте" if miss else "")
                   + ". Файлы сохранены у партии.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("📎 Ещё файлы", f"dw:{key}:{mask}"), btn("📎 Документы партии", f"dc:{key}:0")]])
    await send(uid, "\n".join(out), kb)


async def doc_after_quiet(uid: int, stamp: float, fn) -> None:
    """Подождать, пока придут все файлы, отправленные вместе, и выполнить fn один раз."""
    await asyncio.sleep(DOC_BATCH_WAIT)
    cart = app.doc_cart.get(uid)
    if cart and cart.get("stamp") == stamp:
        await fn()


@dp.message(F.document | F.photo)
async def handle_file(message: Message) -> None:
    uid = message.from_user.id
    if not is_allowed(uid):
        await deny(message)
        return
    if not can_edit(uid) or not has_access(uid, "docs"):
        await message.answer("⛔ Отправлять файлы через бота вам не разрешено. Обратитесь к Генеральному Директору.")
        return
    f = file_of(message)
    if f is None:
        return
    f["note"] = (message.caption or "").strip()
    f["_from_name"] = name_of(message.from_user)
    try:
        await app.refresh()
    except Exception:
        pass                      # таблица сейчас не читается — файл всё равно примем, партии берём из прошлого чтения
    app.pending_input.pop(uid, None)
    cart = doc_cart(uid)
    stamp = time.time()
    if cart and cart.get("rid") and cart.get("wait"):           # после «📎 Дальше — прислать файлы»: отправляем сразу
        cart["ts"] = cart["stamp"] = stamp
        _, got = await doc_deliver(uid, f, cart["rid"], cart["mask"], cart["batch"])
        cart["sent"].append((f, got))

        async def summary():
            sent, cart["sent"] = cart.get("sent", []), []
            await doc_summary(uid, cart["rid"], cart["mask"], sent)
        asyncio.create_task(doc_after_quiet(uid, stamp, summary))
        return
    if cart is None or cart.get("wait"):
        cart = {"files": [], "rid": None, "mask": 0}
        app.doc_cart[uid] = cart
    if len(cart["files"]) >= DOC_CART_MAX:
        await message.answer(f"⚠️ Лимит за один раз: {DOC_CART_MAX}. Сначала отправьте эти файлы.")
        return
    cart["files"].append(f)
    cart["ts"] = cart["stamp"] = stamp

    async def ask():
        if cart.get("rid"):
            text, kb = doc_pick_view(uid, cart["rid"], cart["mask"] or doc_default_mask(uid))
        else:
            text, kb = doc_choose_view(uid)
        await send(uid, text, kb)
    asyncio.create_task(doc_after_quiet(uid, stamp, ask))


@dp.callback_query(F.data.startswith(("dc:", "dg:", "du:", "dw:", "ds:", "dp:", "da:", "dx:", "dy:", "df:")))
@dp.callback_query(F.data.in_({"dn", "dq", "dz", "dd"}))
async def cb_docs(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    uid = cb.from_user.id
    if not has_access(uid, "docs"):
        await cb.answer("⛔ Раздел «Документы» вам не открыт", show_alert=True)
        return
    parts = cb.data.split(":")
    act = parts[0]
    await app.refresh()
    if act in ("du", "dw", "ds", "dp", "dq") and not can_edit(uid):
        await cb.answer("⛔ Наблюдатель не отправляет файлы", show_alert=True)
        return
    if act == "dn":
        text, kb = docs_inbox_view(uid)
        await safe_edit(cb, text, kb)
    elif act == "dc":
        rid = int(parts[1])
        text, kb = docs_view(uid, rid, int(parts[2]) if len(parts) > 2 else 0, parts[3] if len(parts) > 3 else "")
        await safe_edit(cb, text, kb)
    elif act == "dg":
        d = app.db.doc_get(int(parts[1]))
        if d is None or not doc_visible(uid, [d]):
            await cb.answer("Файл не найден", show_alert=True)
            return
        ok = await doc_send_file(uid, d, "📄 <b>Файл из документов партии</b>")
        await cb.answer("" if ok else "⚠️ Файл недоступен в Telegram и копии в папке бота нет", show_alert=not ok)
        return
    elif act == "df":
        d = app.db.doc_get(int(parts[1]))
        if d is None or not can_finalize(uid, d):
            await cb.answer("Уже финальный ✅" if d is not None and d["stage"] == "final" else "⛔ Нельзя", show_alert=True)
            return
        roles = [r for r in (d["to_roles"] or "").split(",") if r]
        if DIRECTOR not in roles:
            roles.append(DIRECTOR)
        app.db.doc_make_final(d["id"], user_name_of(uid), ",".join(roles))
        got = await doc_send_to_roles(uid, d["id"], [DIRECTOR], "final")
        try:
            await cb.message.edit_reply_markup(reply_markup=doc_kb(app.db.doc_get(d["id"]), uid))
        except Exception:
            pass
        if d["from_id"] and d["from_id"] != uid and is_allowed(d["from_id"]):
            await send(d["from_id"], f"✅ <b>{esc(user_name_of(uid))}</b> отметил(а) ваш файл финальным\n"
                                     f"📦 <b>{esc(d['cargo_title'])}</b>\n• {esc(short_title(d['name'], 50))}",
                       InlineKeyboardMarkup(inline_keyboard=[[btn("📎 Документы партии", f"dc:{d['record_id']}:0")]]))
        await cb.answer(f"✅ Финальный · Директору: {len(got)}" if got else "✅ Финальный", show_alert=not got)
        return
    elif act == "du":
        rid, mask = int(parts[1]), int(parts[2]) & 15
        cart = doc_cart(uid)
        if cart and cart.get("files"):
            cart["rid"], cart["mask"], cart["ts"] = rid, mask, time.time()
        text, kb = doc_pick_view(uid, rid, mask)
        await safe_edit(cb, text, kb)
    elif act == "dp":
        rid = int(parts[1])
        cart = doc_cart(uid)
        if not cart or not cart.get("files"):
            await cb.answer("Файлы уже отправлены или время вышло — пришлите их ещё раз.", show_alert=True)
            return
        cart["rid"], cart["mask"], cart["ts"] = rid, cart.get("mask") or doc_default_mask(uid), time.time()
        text, kb = doc_pick_view(uid, rid, cart["mask"])
        await safe_edit(cb, text, kb)
    elif act == "dq":
        ask_input(uid, "doc_find")
        await cb.message.answer("🔎 Напишите название партии, номер ГТД или машины:")
    elif act == "dz":
        app.doc_cart.pop(uid, None)
        app.pending_input.pop(uid, None)
        await safe_edit(cb, "✖ Отменено — файлы никуда не отправлены.", None)
    elif act == "dw":
        rid, mask = int(parts[1]), int(parts[2]) & 15
        if not doc_mask_roles(mask):
            await cb.answer("Отметьте, кому отправить", show_alert=True)
            return
        app.doc_cart[uid] = {"rid": rid, "mask": mask, "wait": True, "batch": doc_batch_id(), "sent": [],
                             "ts": time.time(), "stamp": 0}
        _, _, title = doc_cargo(rid)
        text = ("📎 <b>Пришлите файлы сюда в чат</b> — Excel, PDF, Word, фото. Можно сразу несколько.\n"
                "Подпись к файлу станет комментарием.\n\n"
                f"📦 <b>{esc(title)}</b>\n{DOC_STAGE['final' if mask & DOC_FINAL else 'work']}\n"
                f"➡️ Кому: {doc_to_text(','.join(doc_mask_roles(mask)))}\n\n"
                "Когда закончите — «✅ Готово». Отмена — /cancel.")
        await safe_edit(cb, text, InlineKeyboardMarkup(inline_keyboard=[[btn("✅ Готово", "dd")]]))
    elif act == "dd":
        cart = app.doc_cart.pop(uid, None)
        if cart and cart.get("rid"):
            text, kb = docs_view(uid, cart["rid"])
        else:
            text, kb = docs_inbox_view(uid)
        await safe_edit(cb, text, kb)
    elif act == "ds":
        rid, mask = int(parts[1]), int(parts[2]) & 15
        cart = doc_cart(uid)
        if not cart or not cart.get("files"):
            await cb.answer("Файлы уже отправлены или время вышло — пришлите их ещё раз.", show_alert=True)
            return
        if not doc_mask_roles(mask):
            await cb.answer("Отметьте, кому отправить", show_alert=True)
            return
        files = cart["files"]
        app.doc_cart.pop(uid, None)
        await cb.answer("📤 Отправляю…")
        try:
            await cb.message.delete()
        except Exception:
            pass
        batch, sent = doc_batch_id(), []
        for f in files:
            _, got = await doc_deliver(uid, f, rid, mask, batch)
            sent.append((f, got))
        await doc_summary(uid, rid, mask, sent)
        return
    elif act == "da":
        rows = app.db.doc_ack_batch(int(parts[1]), uid)
        d0 = app.db.doc_get(int(parts[1]))
        if d0 is not None:
            kb_rows = [[btn("✅ Вы приняли", "noop")]]
            if can_finalize(uid, d0):
                kb_rows[0].append(btn("⭐ Сделать финальным", f"df:{d0['id']}"))
            kb_rows.append([btn("📎 Все документы партии", f"dc:{d0['record_id']}:0")])
            try:
                await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
            except Exception:
                pass
        if not rows:
            await cb.answer("Уже отмечено ✅")
            return
        me = user_name_of(uid)
        by_sender: dict[int, list] = {}
        for d in rows:
            if d["from_id"]:
                by_sender.setdefault(d["from_id"], []).append(d)
        for sender, ds in by_sender.items():
            if sender == uid or not is_allowed(sender):
                continue
            text = (f"✅ <b>{esc(me)}</b> · {esc(short_role(uid))} — принял(а) файлы: {len(ds)}\n"
                    f"📦 <b>{esc(ds[0]['cargo_title'])}</b>\n" + "\n".join(f"• {esc(short_title(d['name'], 50))}" for d in ds[:10]))
            await send(sender, text, InlineKeyboardMarkup(inline_keyboard=[[btn("📎 Документы партии", f"dc:{ds[0]['record_id']}:0")]]))
        await cb.answer(f"✅ Принято: {len(rows)}")
        return
    elif act == "dy":
        rid, flt = int(parts[1]), (parts[2] if len(parts) > 2 else "a")
        await cb.answer("🗜 Собираю архив…")
        data, n, skipped = await docs_zip(uid, rid, flt)
        if data is None:
            await cb.message.answer("⚠️ Файлы для архива недоступны (нет копий в папке бота).")
            return
        _, _, title = doc_cargo(rid)
        stamp = datetime.now(cfg.TZ).strftime("%Y-%m-%d")
        kind = "финальные" if flt == "f" else "все"
        cap = f"🗜 <b>{esc(title)}</b> · файлов: {n}" + (f"\n⚠️ Не вошли: {skipped} (слишком большие или нет копии)" if skipped else "")
        await cb.message.answer_document(BufferedInputFile(data, f"UYDA_{safe_name(title)}_{safe_name(i18n.tr(kind, user_lang(uid)))}_{stamp}.zip"),
                                         caption=cap)
        return
    elif act == "dx":
        if not has_access(uid, "excel"):
            await cb.answer("⛔ Выгрузки Excel вам не открыты", show_alert=True)
            return
        key = int(parts[1])
        docs = doc_visible(uid, app.db.docs_all() if key == 0 else app.db.docs_for(doc_cargo(key)[1]))
        if not docs:
            await cb.answer("Файлов пока нет", show_alert=True)
            return
        lang = user_lang(uid)
        title = "Документы партий" if key == 0 else f"Документы · {doc_cargo(key)[2]}"
        stamp = datetime.now(cfg.TZ).strftime("%Y-%m-%d")
        await send_xlsx(cb, docs_xlsx(docs, lang), f"UYDA_{safe_name(title)}_{stamp}.xlsx",
                        f"📥 <b>{esc(title)}</b> · {len(docs)}")
    await cb.answer()


async def doc_reminders() -> None:
    """Раз в день: кому прислали файлы, а он не нажал «✅ Принято» дольше суток — напоминание."""
    users = {r[0] for r in app.db.db.execute("SELECT DISTINCT user_id FROM doc_recv WHERE acked_at IS NULL")}
    for u in users:
        if not is_allowed(u) or not has_access(u, "docs"):
            continue
        old = [d for d in doc_visible(u, app.db.docs_unacked(u)) if time.time() - d["created_at"] > 86400]
        if not old:
            continue
        lines = [f"🔔 <b>Файлы ждут вашего «✅ Принято»: {len(old)}</b>", ""]
        seen = []
        for d in old:
            if d["record_id"] not in seen:
                seen.append(d["record_id"])
                n = sum(1 for x in old if x["record_id"] == d["record_id"])
                lines.append(f"• {esc(short_title(d['cargo_title'], 40))} — {n} · от {esc(d['from_name'])}")
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("📎 Открыть документы", "dn")]])
        await send(u, "\n".join(lines[:20]), kb)


# ===========================================================================
# 🔐 Доступы к разделам (галочки Генерального Директора)
# ===========================================================================
# По умолчанию доступ — по роли. Директор в «👥 Команда» → человек → «🔐 Доступы» включает или выключает
# любой раздел конкретному человеку. Директор и администраторы из env видят всё.
ACCESS_KEYS = [
    ("docs", "📎 Документы", "видит раздел и файлы партий, отправляет файлы"),
    ("docs_work", "🟡 Рабочие файлы", "видит промежуточные версии; без галочки — только финальные"),
    ("dash", "📊 Дашборд", "графики и таблицы по импорту в браузере"),
    ("risks", "⚠️ Риски", "реестр рисков и карточки рисков"),
    ("excel", "📥 Выгрузки Excel", "списки, категории, реестры в Excel"),
    ("week", "📈 Недельный отчёт", "отчёт за неделю по кнопке"),
]
ACCESS_NAME = {k: n for k, n, _ in ACCESS_KEYS}
ACCESS_DEFAULT = {"docs": ACTIVE_ROLES, "docs_work": EDIT_ROLES, "dash": ACTIVE_ROLES, "risks": ACTIVE_ROLES,
                  "excel": ACTIVE_ROLES, "week": ADMIN_ROLES}
# какие кнопки относятся к разделу (кнопки без доступа бот прячет, нажатие — запрещает)
ACCESS_BUTTONS = {
    "docs": ("dn", "dc:", "dg:", "du:", "dw:", "ds:", "dp:", "da:", "dy:", "df:", "dq", "dz", "dd"),
    "risks": ("rk:", "rv:", "rs:", "rp:", "ro:", "rn"),
    "excel": ("xl:", "dx:"),
    "dash": ("db",),
    "week": ("mn:week",),
}


def access_default(uid: int, key: str) -> bool:
    return any(r in ACCESS_DEFAULT.get(key, ()) for r in roles_of(uid))


def access_map(uid: int | None) -> dict[str, bool]:
    """Все доступы человека: {раздел: есть ли доступ}."""
    if uid is None or not is_allowed(uid):
        return {k: False for k, _, _ in ACCESS_KEYS}
    if uid in cfg.OWNER_IDS or can_manage(uid):
        return {k: True for k, _, _ in ACCESS_KEYS}
    over = app.db.access_of(uid)
    return {k: over.get(k, access_default(uid, k)) for k, _, _ in ACCESS_KEYS}


def has_access(uid: int | None, key: str) -> bool:
    return access_map(uid).get(key, False)


def _btn_match(prefix: str, data: str) -> bool:
    return data.startswith(prefix) if prefix.endswith(":") else data == prefix


def button_keys(data: str) -> list[str]:
    return [k for k, prefixes in ACCESS_BUTTONS.items() if any(_btn_match(p, data or "") for p in prefixes)]


def filter_markup(markup, uid: int):
    """Убрать кнопки разделов, к которым у человека нет доступа."""
    if not isinstance(markup, InlineKeyboardMarkup):
        return markup
    acc = access_map(uid)
    rows = []
    for row in markup.inline_keyboard:
        kept = [b for b in row if not b.callback_data or all(acc.get(k, True) for k in button_keys(b.callback_data))]
        if kept:
            rows.append(kept)
    if len(rows) == len(markup.inline_keyboard) and all(len(a) == len(b) for a, b in zip(rows, markup.inline_keyboard)):
        return markup
    return markup.model_copy(update={"inline_keyboard": rows})


def access_view(target: int, page: int, viewer: int) -> tuple[str, InlineKeyboardMarkup]:
    m = next((x for x in team_members() if x["id"] == target), None)
    back = [btn("← К человеку", f"tu:{target}:{page}")]
    if m is None:
        return "Пользователь не найден.", InlineKeyboardMarkup(inline_keyboard=[back])
    role = role_of(target)
    over = app.db.access_of(target)
    out = [f"🔐 <b>Доступы</b> · {esc(member_title(m))}", f"Роль: <b>{role_label(role) if role else '—'}</b>", ""]
    full = target in cfg.OWNER_IDS or can_manage(target)
    if full:
        out.append("🔒 У Генерального Директора и администратора из env — все разделы, менять не нужно.")
    else:
        out.append("Нажмите на раздел, чтобы включить или выключить. «по роли» — как у всех с этой ролью, "
                   "«вручную» — вы изменили.")
    out.append("")
    acc = access_map(target)
    kb = []
    for key, name, info in ACCESS_KEYS:
        on = acc[key]
        how = "вручную" if key in over else "по роли"
        out.append(f"{'☑️' if on else '▫️'} <b>{name}</b> — {info} <i>({how})</i>")
        if not full:
            kb.append([btn(("☑️ " if on else "▫️ ") + name + (" ✍️" if key in over else ""), f"ut:{target}:{key}:{page}")])
    if over and not full:
        kb.append([btn("↺ Как у роли (сбросить)", f"ur:{target}:{page}")])
    kb.append(back)
    return "\n".join(out), InlineKeyboardMarkup(inline_keyboard=kb)


@dp.callback_query(F.data.startswith(("ua:", "ut:", "ur:")))
async def cb_access(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    uid = cb.from_user.id
    if not can_manage(uid):
        await cb.answer("⛔ Доступы меняет только Генеральный Директор", show_alert=True)
        return
    parts = cb.data.split(":")
    act, target = parts[0], int(parts[1])
    if act == "ut":
        key, page = parts[2], int(parts[3])
        if key in ACCESS_NAME and not (target in cfg.OWNER_IDS or can_manage(target)):
            new = not has_access(target, key)
            app.db.access_set(target, key, None if new == access_default(target, key) else new, user_name_of(uid))
            if key == "docs":
                await refresh_menu(target)
            await cb.answer(f"{ACCESS_NAME[key]}: {'включено' if new else 'выключено'}")
        else:
            await cb.answer()
    elif act == "ur":
        page = int(parts[2])
        app.db.access_reset(target)
        await refresh_menu(target)
        await cb.answer("↺ Доступы — как у роли")
    else:
        page = int(parts[2])
        await cb.answer()
    text, kb = access_view(target, page, uid)
    await safe_edit(cb, text, kb)


async def refresh_menu(uid: int) -> None:
    """Обновить меню внизу экрана у человека (после смены доступа к «📎 Документы»)."""
    if not is_allowed(uid):
        return
    try:
        await app.bot.send_message(uid, "🔐 Ваши доступы в боте обновлены.", reply_markup=main_menu(uid))
    except Exception:
        pass


# ===========================================================================
# 📊 Дашборд — HTML-страница с графиками и таблицами
# ===========================================================================
def dashboard_data(uid: int, lang: str) -> dict:
    T = lambda s: i18n.tr(s, lang)
    now = datetime.now(cfg.TZ)
    cargos = all_cargos()
    open_c = [c for c in cargos if is_open_cargo(c)]
    num_ = lambda v: float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

    # по месяцам: последние 12 (по дате ГТД ИМ40)
    months = []
    y, mo = now.year, now.month
    keys = []
    for _ in range(12):
        keys.append((y, mo))
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    keys.reverse()
    by_m = {k: {"n": 0, "factory": 0.0, "overhead": 0.0, "fx": 0.0, "ox": 0.0} for k in keys}
    for c in cargos:
        d = c.get("date")
        if not d or (d.year, d.month) not in by_m:
            continue
        b = by_m[(d.year, d.month)]
        a = c["agg"]
        b["n"] += 1
        f, o = num_(a.get("factory_usd")), num_(a.get("calc_overhead_usd"))
        b["factory"] += f
        b["overhead"] += o
        if f and "calc_overhead_usd" in a:      # процент — только по партиям, где есть и завод, и расходы
            b["fx"] += f
            b["ox"] += o
    for (yy, mm) in keys:
        b = by_m[(yy, mm)]
        pct = None
        if b["fx"]:
            pct = round(b["ox"] / b["fx"] * 100, 1)
        months.append({"label": f"{T(MONTHS[mm - 1])[:3]} {str(yy)[2:]}", "n": b["n"], "factory": round(b["factory"]),
                       "overhead": round(b["overhead"]), "pct": pct})

    # категории за 12 месяцев
    year_c = [c for c in cargos if c.get("date") and (now.date() - c["date"]).days <= 365]
    cats = {}
    for dim in ("c", "s", "m", "t", "p"):
        agg: dict[str, dict] = {}
        for c in year_c:
            key, label = c["keys"][dim]
            r = agg.setdefault(key, {"label": T(label), "n": 0, "factory": 0.0, "overhead": 0.0, "fx": 0.0, "ox": 0.0})
            a = c["agg"]
            f, o = num_(a.get("factory_usd")), num_(a.get("calc_overhead_usd"))
            r["n"] += 1
            r["factory"] += f
            r["overhead"] += o
            if f and "calc_overhead_usd" in a:
                r["fx"] += f
                r["ox"] += o
        rows = sorted(agg.values(), key=lambda r: (-(r["factory"] + r["overhead"]), -r["n"]))
        total = sum(r["n"] for r in rows) or 1
        cats[T(ct.DIMENSIONS[dim][1])] = [{"label": r["label"], "n": r["n"], "factory": round(r["factory"]),
                                           "overhead": round(r["overhead"]),
                                           "pct": round(r["ox"] / r["fx"] * 100, 1) if r["fx"] else None,
                                           "share": round(r["n"] / total * 100, 1)} for r in rows[:40]]

    # открытые партии
    risk_by, _pf = risk_scan() if has_access(uid, "risks") else ({}, [])
    rows_open = []
    for c in sorted(open_c, key=lambda c: c.get("date") or datetime.min.date(), reverse=True):
        rid = c["ids"][0]
        a = c["agg"]
        role = next((app.resp_map.get(i) for i in c["ids"] if app.resp_map.get(i)), "")
        sd = max((stage_days(i) or 0 for i in c["ids"]), default=0) if role else None
        cr = risk_by.get(rid)
        total, _w = app.db.doc_counts(c["ids"])
        finals = sum(1 for d in app.db.docs_for(c["ids"]) if d["stage"] == "final") if total else 0
        missing = sum(len(p.get("missing", [])) for p in c["parts"])
        yellow = sum(len(p.get("unconfirmed", [])) for p in c["parts"])
        rows_open.append({
            "title": cargo_name(c), "date": c["date"].strftime("%d.%m.%Y") if c.get("date") else "",
            "regime": T(c["keys"]["m"][1].split(" · ")[0]), "country": T(c["keys"]["c"][1]), "supplier": c["keys"]["s"][1],
            "resp": T(RESP_TITLE.get(role, "—")) if role else "—", "stage": sd, "norm": sla_norm(role) if role else None,
            "late": bool(role and sd is not None and sd > sla_norm(role)), "missing": missing, "yellow": yellow,
            "risk": cr.score if cr and cr.score else 0, "risk_icon": rk.level(cr.score)[0] if cr and cr.score else "",
            "docs": total, "finals": finals,
            "factory": round(num_(a.get("factory_usd"))) or None, "overhead": round(num_(a.get("calc_overhead_usd"))) or None,
            "pct": round(a["calc_overhead_pct"], 1) if isinstance(a.get("calc_overhead_pct"), (int, float)) else None,
            "landed": round(num_(a.get("calc_landed_usd"))) or None})

    # риски
    risks_rows = []
    if has_access(uid, "risks"):
        for r in app.db.risks(("open", "work")):
            score = r["p"] * r["i"]
            risks_rows.append({"icon": rk.level(score)[0], "score": score, "kind": T(rk.KINDS.get(r["kind"], ("", "Риск"))[1]),
                               "title": r["title"], "why": T(r["why"] or ""), "owner": T(OWNER_NAME.get(r["owner"], r["owner"])),
                               "status": T(RISK_STATUS.get(r["status"], ("", r["status"]))[1])})
        risks_rows.sort(key=lambda x: -x["score"])

    # документы
    docs_rows = []
    if has_access(uid, "docs"):
        docs = doc_visible(uid, app.db.docs_all())[:300]
        recv = app.db.doc_recv_map([d["id"] for d in docs])
        for d in docs:
            rs = recv.get(d["id"], [])
            docs_rows.append({"date": fmt_dt(d["created_at"]), "cargo": d["cargo_title"], "name": d["name"],
                              "stage": T(DOC_STAGE.get(d["stage"], "")), "from": d["from_name"],
                              "to": T(doc_to_text(d["to_roles"])),
                              "acc": ", ".join(r["user_name"] for r in rs if r["acked_at"]) or (T("ждёт") if rs else "—")})

    # показатели
    resp_of = lambda c: next((app.resp_map.get(i) for i in c["ids"] if app.resp_map.get(i)), "")
    counts = {"k": sum(1 for c in open_c if resp_of(c) == CUSTOMS), "u": sum(1 for c in open_c if resp_of(c) == OPERATOR),
              "d": sum(1 for c in open_c if any(stage_overdue(i) for i in c["ids"]))}
    rc = risk_counts() if has_access(uid, "risks") else {"red": 0, "orange": 0}
    this_m, prev_m = months[-1], months[-2]
    waiting_docs = app.db.db.execute("SELECT COUNT(DISTINCT doc_id) FROM doc_recv WHERE acked_at IS NULL").fetchone()[0]
    change = ""
    if prev_m["overhead"]:
        change = f"{(this_m['overhead'] - prev_m['overhead']) / prev_m['overhead'] * 100:+.0f}% " + T("к прошлому месяцу")
    kpi = [
        {"label": T("Открытые партии"), "value": str(len(open_c)),
         "sub": f"🛃 {counts['k']} · 🧾 {counts['u']}"},
        {"label": T("Срок этапа прошёл"), "value": str(counts["d"]), "tone": "bad" if counts["d"] else "good"},
        {"label": T("Расходы за месяц"), "value": "$" + f"{this_m['overhead']:,.0f}".replace(",", " "), "sub": change},
        {"label": T("Завод за месяц"), "value": "$" + f"{this_m['factory']:,.0f}".replace(",", " "),
         "sub": f"{T('Партий')}: {this_m['n']}"},
        {"label": T("Расходы к заводу"), "value": (f"{this_m['pct']:.1f}%".replace(".", ",") if this_m["pct"] is not None else "—"),
         "sub": (T("прошлый месяц") + f": {prev_m['pct']:.1f}%".replace(".", ",")) if prev_m["pct"] is not None else ""},
    ]
    if has_access(uid, "risks"):
        kpi.append({"label": T("Риски"), "value": f"🔴 {rc['red']} · 🟠 {rc['orange']}",
                    "tone": "bad" if rc["red"] else ("warn" if rc["orange"] else "good")})
    if has_access(uid, "docs"):
        kpi.append({"label": T("Файлы ждут «Принято»"), "value": str(waiting_docs), "tone": "warn" if waiting_docs else "good"})

    t = {k: T(v) for k, v in {
        "title": "Uyda · Импорт — дашборд", "tab_overview": "Обзор", "tab_open": "Открытые партии",
        "tab_cats": "Категории", "tab_risks": "Риски", "tab_docs": "Документы",
        "ch_money": "Завод и расходы по месяцам", "ch_money_hint": "USD, по дате ГТД ИМ40, последние 12 месяцев",
        "ch_pct": "Расходы к цене завода", "ch_pct_hint": "%, по месяцам, только партии с ценой завода — чем ниже, тем лучше",
        "ch_cat": "Завод и расходы по категориям", "ch_cat_hint": "USD, последние 12 месяцев, крупнейшие 12",
        "factory": "Завод", "overhead": "Расходы", "landed": "Себестоимость", "pct": "Расходы к заводу",
        "cargos": "Партий", "cargo": "Партия", "share": "Доля партий", "date": "Дата", "resp": "Ответственный",
        "stage": "Дней на этапе / норма", "missing": "Не заполнено", "risk": "Риск", "score": "Балл", "kind": "Риск",
        "about": "О чём", "why": "Почему", "owner": "Кто отвечает", "status": "Статус", "file": "Файл",
        "dstage": "Этап", "from": "От кого", "to": "Кому", "acc": "Приняли", "search": "Поиск…",
        "empty": "Нет данных", "nochart": "Графики не загрузились — откройте файл в браузере (Chrome, Safari, Edge).",
        "footer": "Uyda · Shinam • Qulay • Sifatli — данные из таблицы импорта. Бот только читает таблицу и 1С.",
    }.items()}
    t["generated"] = T("Данные на") + " " + now.strftime("%d.%m.%Y %H:%M")
    return {"lang": lang, "t": t, "kpi": kpi, "months": months, "cats": cats, "open": rows_open,
            "risks": risks_rows, "docs": docs_rows}


@dp.message(Command("dash"))
async def cmd_dash(message: Message) -> None:
    async def run():
        uid = message.from_user.id
        if not has_access(uid, "dash"):
            await message.answer("⛔ Дашборд вам не открыт. Обратитесь к Генеральному Директору.")
            return
        await app.refresh()
        await send_dashboard(uid)
    await guarded(message, run)


@dp.callback_query(F.data == "db")
async def cb_dash(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    if not has_access(cb.from_user.id, "dash"):
        await cb.answer("⛔ Дашборд вам не открыт", show_alert=True)
        return
    await cb.answer("📊 Собираю дашборд…")
    await app.refresh()
    await send_dashboard(cb.from_user.id)


async def send_dashboard(uid: int) -> None:
    import dashboard
    lang = user_lang(uid)
    page = dashboard.build_html(dashboard_data(uid, lang))
    stamp = datetime.now(cfg.TZ).strftime("%Y-%m-%d_%H%M")
    await app.bot.send_document(
        uid, BufferedInputFile(page.encode("utf-8"), f"UYDA_dashboard_{stamp}.html"),
        caption="📊 <b>Дашборд</b> — откройте файл: графики, открытые партии с поиском, категории, риски, документы.\n"
                "<i>На телефоне: нажмите на файл → «Открыть в…» → Chrome или Safari. Интернет не нужен.</i>")


# ===========================================================================
# 🌐 Три языка: русский, o‘zbekcha, English
# ===========================================================================
MENU_TEXTS = {MENU_TODAY, MENU_OPEN, MENU_CATS, MENU_DONE, MENU_SEARCH, MENU_TOTALS, MENU_CHECKS, MENU_HELP,
              MENU_TEAM, MENU_SUPPORT, MENU_REMARKS, MENU_LANG, MENU_OVERDUE, MENU_MORE, MENU_DOCS, "☰ Ещё", "❓ Помощь"}


def user_lang(uid: int | None) -> str:
    if not uid:
        return i18n.DEFAULT
    return app.db.get_lang(uid) or i18n.DEFAULT


def lang_kb(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn(("✓ " if k == current else "") + v, f"lg:{k}")]
                                                 for k, v in i18n.LANGS.items()])


LANG_PROMPT = "🌐 <b>Tilni tanlang · Выберите язык · Choose language</b>"


def _tr_markup(markup, lang: str):
    """Перевод текста кнопок (inline и меню внизу экрана)."""
    if markup is None:
        return None
    if isinstance(markup, InlineKeyboardMarkup):
        rows = [[b.model_copy(update={"text": i18n.tr(b.text, lang)}) for b in row] for row in markup.inline_keyboard]
        return markup.model_copy(update={"inline_keyboard": rows})
    if isinstance(markup, ReplyKeyboardMarkup):
        rows = [[b.model_copy(update={"text": i18n.tr(b.text, lang)}) for b in row] for row in markup.keyboard]
        return markup.model_copy(update={"keyboard": rows,
                                         "input_field_placeholder": i18n.tr(markup.input_field_placeholder, lang)})
    if isinstance(markup, ForceReply):
        return markup.model_copy(update={"input_field_placeholder": i18n.tr(markup.input_field_placeholder, lang)})
    return markup


class TranslateOutgoing(BaseRequestMiddleware):
    """Каждое исходящее сообщение, подпись и кнопка — на языке получателя."""
    async def __call__(self, make_request, bot, method):
        try:
            if isinstance(getattr(method, "reply_markup", None), InlineKeyboardMarkup) and \
                    type(method).__name__ in ("SendMessage", "EditMessageText", "SendPhoto"):
                method = method.model_copy(update={"reply_markup": with_nav(method.reply_markup)})
        except Exception:
            log.exception("Не удалось добавить кнопки навигации")
        try:
            chat = getattr(method, "chat_id", None)
            if isinstance(chat, int) and chat > 0 and isinstance(getattr(method, "reply_markup", None), InlineKeyboardMarkup):
                method = method.model_copy(update={"reply_markup": filter_markup(method.reply_markup, chat)})
        except Exception:
            log.exception("Не удалось применить доступы к кнопкам")
        try:
            chat = getattr(method, "chat_id", None)
            lang = user_lang(chat) if isinstance(chat, int) and chat > 0 else i18n.CURRENT.get()
            if True:   # ru — замена «груз» → «партия», uz/en — перевод
                upd = {}
                for field in ("text", "caption"):
                    v = getattr(method, field, None)
                    if isinstance(v, str):
                        upd[field] = i18n.tr(v, lang)
                if getattr(method, "reply_markup", None) is not None:
                    upd["reply_markup"] = _tr_markup(method.reply_markup, lang)
                if upd:
                    method = method.model_copy(update=upd)
        except Exception:
            log.exception("Перевод сообщения не удался — отправляю как есть")
        return await make_request(bot, method)


@dp.callback_query.outer_middleware()
async def nav_history(handler, event: CallbackQuery, data):
    """Запоминает путь по экранам. «◀ Назад» превращается в нажатие предыдущего экрана."""
    uid, cd = event.from_user.id, event.data or ""
    denied = [k for k in button_keys(cd) if not has_access(uid, k)]
    if denied and is_allowed(uid):
        await event.answer(f"⛔ {ACCESS_NAME[denied[0]]} — раздел вам не открыт. Обратитесь к Генеральному Директору.",
                           show_alert=True)
        return
    stack = app.nav.setdefault(uid, [])
    if cd in ("bk", "hm"):
        cart = app.doc_cart.get(uid)
        if cart and cart.get("wait"):      # ушли с экрана «Пришлите файлы» — больше не ждём файлы
            app.doc_cart.pop(uid, None)
    if cd == "bk":
        if stack:
            stack.pop()                      # текущий экран
        if stack:
            event = event.model_copy(update={"data": stack[-1]})
        else:
            event = event.model_copy(update={"data": "hm"})
        return await handler(event, data)
    if cd == "hm":
        app.nav[uid] = []
    elif not cd.startswith(NAV_SKIP):
        if cd.startswith("du:") and stack and stack[-1].startswith("du:"):
            stack[-1] = cd                   # галочки получателей — тот же экран
        elif not stack or stack[-1] != cd:
            stack.append(cd)
            del stack[:-30]
    return await handler(event, data)


@dp.callback_query(F.data == "hm")
async def cb_home(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    app.pending_input.pop(cb.from_user.id, None)
    await app.refresh()
    text, kb = home_view(cb.from_user.id)
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.update.outer_middleware()
async def lang_context(handler, event, data):
    """Язык текущего пользователя (для таблиц и картинок) + кнопки меню на другом языке -> русский."""
    user = data.get("event_from_user")
    token = None
    if user is not None:
        lang = app.db.get_lang(user.id)
        if lang is None:
            lang = i18n.detect(user.language_code)
            if is_allowed(user.id):
                app.db.set_lang(user.id, lang)
        token = i18n.CURRENT.set(lang)
        msg = event.message
        if msg is not None and msg.text and lang != "ru":
            ru = i18n.back_to_ru(msg.text)
            if ru != msg.text and ru in MENU_TEXTS:   # только кнопки меню, обычный текст не трогаем
                event = event.model_copy(update={"message": msg.model_copy(update={"text": ru})})
    try:
        return await handler(event, data)
    finally:
        if token is not None:
            i18n.CURRENT.reset(token)


@dp.message(Command("lang", "til", "language"))
@dp.message(F.text == MENU_LANG)
async def cmd_lang(message: Message) -> None:
    await message.answer(LANG_PROMPT, reply_markup=lang_kb(user_lang(message.from_user.id)))


@dp.callback_query(F.data.startswith("lg:"))
async def cb_lang(cb: CallbackQuery) -> None:
    lang = cb.data.split(":", 1)[1]
    if lang not in i18n.LANGS:
        await cb.answer()
        return
    app.db.set_lang(cb.from_user.id, lang)
    i18n.CURRENT.set(lang)
    done = {"ru": "✅ Язык: Русский", "uz": "✅ Til: O‘zbekcha", "en": "✅ Language: English"}[lang]
    await cb.answer(done)
    try:
        await cb.message.edit_text(LANG_PROMPT, reply_markup=lang_kb(lang))
    except TelegramBadRequest:
        pass
    if is_allowed(cb.from_user.id):
        await cb.message.answer(done, reply_markup=main_menu(cb.from_user.id))


# ===========================================================================
# 📌 Замечания Генерального Директора и 🆘 окно поддержки
# ===========================================================================
def name_of(user) -> str:
    return user.full_name or (f"@{user.username}" if user.username else f"ID {user.id}")


def short_role(uid: int) -> str:
    r = role_of(uid)
    return ROLE_SHORT.get(r, ROLE_NAME.get(r, "")) if r else ""


def remarks_view(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = app.db.open_remarks()
    by_rid: dict[int, list] = {}
    for r in rows:
        by_rid.setdefault(r["record_id"], []).append(r)
    out = [f"📌 <b>Замечания Генерального Директора</b> · открыто {len(rows)}"]
    kb = []
    if not rows:
        out.append("\nОткрытых замечаний нет 👍")
    else:
        out.append("\nНажмите на груз — откроется карточка, там можно нажать «👍 Принято».")
        for rid, items in list(by_rid.items())[:20]:
            row = app.db.get(rid)
            if not row:
                continue
            who = RESP_ICON.get(app.resp_map.get(rid, ""), "")
            out.append(f"\n{who} <b>{esc(short_title(row['title'], 40))}</b>")
            for r in items[-2:]:
                out.append(f"<blockquote>{esc(r['text'][:200])}\n<i>{fmt_dt(r['created_at'])}</i></blockquote>")
            kb.append([btn(f"📌 {short_title(row['title'], 28)} · {len(items)}", f"c:{rid}:-")])
    return fit("\n".join(out)), InlineKeyboardMarkup(inline_keyboard=kb)


async def notify_remark(rid: int, author_id: int, text: str) -> int:
    """Замечание Директора: ответственному, 1С УНФ-Оператору и Разработчику (кроме автора)."""
    role = app.resp_map.get(rid, "")
    targets = set(app.db.db_ids_by_roles((OPERATOR, DEVELOPER)))
    if role:
        targets |= resp_targets(role)
    targets = {u for u in targets if is_allowed(u)} - {author_id}
    row = app.db.get(rid)
    msg = (f"📌 <b>Замечание Генерального Директора</b>\n\n📦 <b>{esc(row['title'])}</b>\n"
           f"<blockquote>{esc(text)}</blockquote>")
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("📦 Открыть карточку", f"c:{rid}:-"),
                                                btn("👍 Принято", f"ra:{rid}:-")]])
    for u in targets:
        await send(u, msg, kb)
    return len(targets)


def support_staff() -> set[int]:
    """Кто отвечает в окне поддержки и наблюдает: Разработчик, Директор, администраторы."""
    return admin_ids()


def thread_text(tid: int, viewer: int) -> str:
    th = app.db.thread_get(tid)
    out = [f"🆘 <b>Обращение №{tid}</b> · {'🟢 открыто' if th['status'] == 'open' else '✅ решено'}",
           f"👤 {esc(th['user_name'])}"]
    if th["record_id"] and app.db.get(th["record_id"]):
        out.append(f"📦 {esc(app.db.get(th['record_id'])['title'])}")
    out.append("")
    for m in app.db.msgs(tid, 8):
        mark = "📌 Замечание Директора" if m["kind"] == "remark" else esc(m["from_name"])
        role = f" ({esc(m['from_role'])})" if m["from_role"] and m["kind"] != "remark" else ""
        out.append(f"<b>{mark}</b>{role} · {fmt_dt(m['ts'])}\n<blockquote>{esc(m['text'][:400])}</blockquote>")
    return fit("\n".join(out))


def thread_kb(tid: int, viewer: int) -> InlineKeyboardMarkup:
    th = app.db.thread_get(tid)
    staff = viewer in support_staff()
    rows = []
    if th["status"] == "open":
        if viewer == th["user_id"]:
            rows.append([btn("✍️ Написать", f"sp:w:{tid}"), btn("✅ Решено", f"sp:c:{tid}")])
        if staff and viewer != th["user_id"]:
            rows.append([btn("↩ Ответить", f"sp:r:{tid}"), btn("✅ Решено", f"sp:c:{tid}")])
        if can_manage(viewer) and viewer != th["user_id"]:
            rows.append([btn("📌 Замечание", f"sp:m:{tid}")])
    if th["record_id"]:
        rows.append([btn("📦 Карточка груза", f"c:{th['record_id']}:-")])
    if staff:
        rows.append([btn("◀ Все обращения", "sp:l")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support_view(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    if uid in support_staff():
        threads = app.db.threads_open(15)
        out = [f"🆘 <b>Поддержка</b> · открытых обращений: {len(threads)}"]
        out.append("" if threads else "\nОткрытых обращений нет 👍")
        kb = []
        for th in threads:
            kb.append([btn(f"💬 №{th['id']} · {short_title(th['user_name'] or '—', 14)} · {short_title(th['topic'] or '—', 18)}",
                           f"sp:v:{th['id']}")])
        kb.append([btn("✍️ Задать свой вопрос", "sp:w:0")])
        return "\n".join(out), InlineKeyboardMarkup(inline_keyboard=kb)
    th = app.db.thread_open_of(uid)
    if th:
        return thread_text(th["id"], uid), thread_kb(th["id"], uid)
    text = ("🆘 <b>Поддержка</b>\n\nЕсли что-то не получается в 1С УНФ или в боте — напишите вопрос. "
            "Разработчик и Генеральный Директор получат его сразу и ответят прямо здесь.\n\n"
            "Вопрос по конкретному грузу можно задать кнопкой «🆘 Помощь» на карточке груза.")
    return text, InlineKeyboardMarkup(inline_keyboard=[[btn("✍️ Написать в поддержку", "sp:w:0")]])


async def relay_support(tid: int, sender_id: int, text: str, kind: str, sname: str = "") -> int:
    """Рассылка сообщения из обращения. Вопрос -> Разработчику и Директору; ответ/замечание -> автору и наблюдателям."""
    th = app.db.thread_get(tid)
    asker = th["user_id"]
    staff = support_staff() - {sender_id}
    head_cargo = ""
    if th["record_id"] and app.db.get(th["record_id"]):
        head_cargo = f"\n📦 {esc(app.db.get(th['record_id'])['title'])}"
    sname = sname or f"ID {sender_id}"
    sent = 0
    if kind == "remark":
        title = "📌 <b>Замечание Генерального Директора</b>"
    elif sender_id == asker:
        title = f"🆘 <b>Вопрос в поддержку №{tid}</b>"
    else:
        title = f"💬 <b>Ответ поддержки №{tid}</b>"
    body = f"{title}\n👤 {esc(sname)} · {esc(short_role(sender_id))}{head_cargo}\n<blockquote>{esc(text)}</blockquote>"
    if sender_id == asker:
        targets = staff
    else:
        targets = (staff | {asker}) - {sender_id}
    for u in targets:
        if u == asker:
            kb = InlineKeyboardMarkup(inline_keyboard=[[btn("✍️ Ответить", f"sp:w:{tid}"), btn("✅ Решено", f"sp:c:{tid}")]])
        else:
            rows = [[btn("↩ Ответить", f"sp:r:{tid}"), btn("💬 Переписка", f"sp:v:{tid}")]]
            if can_manage(u) and u != asker:
                rows.append([btn("📌 Замечание", f"sp:m:{tid}")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
        await send(u, body, kb)
        sent += 1
    return sent


def ask_input(uid: int, *state) -> None:
    app.pending_input[uid] = (*state, time.time())


async def handle_input(message: Message, pi: tuple) -> None:
    """Текст после кнопки «Замечание» / «Написать» / «Ответить»."""
    uid = message.from_user.id
    kind, text = pi[0], (message.text or "").strip()[:1500]
    if not text:
        return
    me = name_of(message.from_user)
    if kind == "rm":                                   # замечание по грузу
        rid, ctx = pi[1], pi[2]
        if not can_manage(uid):
            return
        app.db.add_remark(rid, uid, me, text)
        n = await notify_remark(rid, uid, text)
        await message.answer(card_html(rid, f"📌 <b>Замечание отправлено</b> · получателей: {n}"),
                             reply_markup=card_kb(rid, ctx, uid))
        return
    if kind == "rk_new" and can_risk_edit(uid):          # новый риск вручную
        rid_ = app.db.risk_add(text[:300], 3, 3, "director", me)
        t, kb = risk_view(uid, rid_)
        await message.answer("✅ <b>Риск добавлен</b> — задайте вероятность, влияние и ответственного.\n\n" + t,
                             reply_markup=kb)
        return
    if kind == "rk_note" and can_edit(uid):              # снят / принят — с комментарием
        rid_, status = pi[1], pi[2]
        app.db.risk_update(rid_, me, status=status, note=text[:500])
        t, kb = risk_view(uid, rid_)
        await message.answer(t, reply_markup=kb)
        r = app.db.risk_get(rid_)
        if status == "accepted" or r["p"] * r["i"] >= 15:
            for u in manager_ids() - {uid}:
                await send(u, f"{RISK_STATUS[status][0]} <b>Риск {RISK_STATUS[status][1].lower()}</b> · {esc(me)}\n"
                              f"{esc(rk.KINDS.get(r['kind'], ('', 'Риск'))[1])} · {esc(r['title'])}\n"
                              f"<blockquote>{esc(text[:300])}</blockquote>",
                           InlineKeyboardMarkup(inline_keyboard=[[btn("⚠️ Открыть риск", f"rv:{rid_}")]]))
        return
    if not can_edit(uid):
        return
    if kind == "doc_find":                             # к какой партии отнести присланные файлы
        await app.refresh()
        hits = sh.search_cargo(app.records, text) or sh.search_free(app.records, text)
        found = []
        for r in hits:
            key = doc_cargo(r["id"])[0]
            if key not in found:
                found.append(key)
        t, kb = doc_choose_view(uid, found)
        await message.answer(t, reply_markup=kb)
        return
    if kind == "sp":                                   # вопрос в поддержку
        tid, rid = pi[1], pi[2]
        th = app.db.thread_get(tid) if tid else None
        if th is None or th["status"] != "open":
            th_open = app.db.thread_open_of(uid)
            if th_open and rid and app.db.get(rid):
                text = f"[{app.db.get(rid)['title']}] {text}"
            tid = th_open["id"] if th_open else app.db.thread_new(uid, me, rid, text)
        app.db.msg_add(tid, uid, me, short_role(uid), text)
        n = await relay_support(tid, uid, text, "msg", me)
        note = "" if n else "\n⚠️ Некому доставить: в боте нет Разработчика / Директора."
        await message.answer(f"✅ <b>Отправлено в поддержку</b> (обращение №{tid}){note}\n"
                             "Ответ придёт сюда.", reply_markup=thread_kb(tid, uid))
        return
    th = app.db.thread_get(pi[1])
    if th is None or uid not in support_staff():
        return
    if kind == "spm" and not can_manage(uid):
        return
    k = "remark" if kind == "spm" else "msg"
    app.db.msg_add(th["id"], uid, me, short_role(uid), text, k)
    n = await relay_support(th["id"], uid, text, k, me)
    await message.answer(f"✅ Отправлено · получателей: {n}", reply_markup=thread_kb(th["id"], uid))


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    app.pending_input.pop(message.from_user.id, None)
    app.pending_reason.pop(message.from_user.id, None)
    app.doc_cart.pop(message.from_user.id, None)
    await message.answer("Отменено.")


@dp.message(Command("remarks"))
@dp.message(F.text == MENU_REMARKS)
async def cmd_remarks(message: Message) -> None:
    async def run():
        app.pending_input.pop(message.from_user.id, None)
        await app.refresh()
        text, kb = remarks_view(message.from_user.id)
        await screen(message, text, kb, "mn:rem")
    await guarded(message, run)


@dp.message(Command("docs"))
@dp.message(F.text == MENU_DOCS)
async def cmd_docs(message: Message) -> None:
    async def run():
        app.pending_input.pop(message.from_user.id, None)
        if not has_access(message.from_user.id, "docs"):
            await message.answer("⛔ Раздел «Документы» вам не открыт. Обратитесь к Генеральному Директору.")
            return
        await app.refresh()
        text, kb = docs_inbox_view(message.from_user.id)
        await screen(message, text, kb, "dn")
    await guarded(message, run)


@dp.message(Command("support"))
@dp.message(F.text == MENU_SUPPORT)
async def cmd_support(message: Message) -> None:
    async def run():
        app.pending_input.pop(message.from_user.id, None)
        if not can_edit(message.from_user.id):
            await message.answer("⛔ Наблюдатель не пишет в поддержку. Обратитесь к Генеральному Директору.")
            return
        text, kb = support_view(message.from_user.id)
        await screen(message, text, kb, "mn:sup")
    await guarded(message, run)


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message) -> None:
    uid = message.from_user.id
    pi = app.pending_input.pop(uid, None)
    if pi and is_allowed(uid) and time.time() - pi[-1] < INPUT_TTL:
        await handle_input(message, pi)
        return
    if uid in app.pending_reason and not can_edit(uid):
        app.pending_reason.pop(uid, None)   # роль могли сменить на «Наблюдатель», пока ждали причину
    if uid in app.pending_reason:
        rid, ctx = app.pending_reason.pop(uid)
        old = app.db.get(rid)["status"]
        app.db.set_status(rid, FAIL, reason=message.text.strip()[:500], user_id=uid,
                          user_name=message.from_user.full_name)
        await message.answer(card_html(rid, "✍️ <b>Причина сохранена</b>"), reply_markup=card_kb(rid, ctx, uid))
        await notify_owners(rid, uid, old)
        return

    if message.text.strip().lower().lstrip("/") in ("start", "старт", "меню", "menu", "начать"):
        await cmd_start(message)
        return

    async def run():
        records = await app.refresh()
        query = message.text.strip()
        # Сначала — по названию груза (столбец A); затем — категория («Китай», «ПОСУДА», «2025»);
        # затем — по остальным полям (ГТД, авто, ККДГ…)
        found = sh.search_cargo(records, query)
        if not found:
            hit = ct.find_by_label(cat_data("a")[1], query)
            if hit:
                text, kb = cat_view(uid, hit[0], hit[1], 0, "a")
                await message.answer(text, reply_markup=kb)
                return
            found = sh.search_free(records, query)
        await show_results(message, found, query)
    await guarded(message, run)


# ===========================================================================
# Кнопки
# ===========================================================================
async def allowed_cb(cb: CallbackQuery) -> bool:
    if not is_allowed(cb.from_user.id):
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return False
    return True


async def record_cb(cb: CallbackQuery) -> int | None:
    if not await allowed_cb(cb):
        return None
    rid = int(cb.data.split(":")[1])
    if not app.db.get(rid):
        await cb.answer("Груз не найден", show_alert=True)
        return None
    return rid


@dp.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@dp.callback_query(F.data.startswith(("l:", "r:")))
async def cb_list(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    kind, f, page = cb.data.split(":")
    if kind == "r":
        await app.refresh(force=True)
    text, kb = list_view(f, int(page))
    await safe_edit(cb, text, kb)
    await cb.answer("Обновлено" if kind == "r" else None)


@dp.callback_query(F.data.startswith("dn:"))
async def cb_done(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    _, ym, page = cb.data.split(":")
    text, kb = done_view(cb.from_user.id, ym, int(page))
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith(("g:", "gv:")))
async def cb_group(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    kind, gi = cb.data.split(":")[:2]
    gi = int(gi)
    if kind == "gv":   # весь груз: валюты и виды расходов по всем частям
        g = group_recs(cb.from_user.id, gi)
        if not g:
            text, kb = group_view(cb.from_user.id, gi)
        else:
            text = breakdown_html(g, f"📦 <b>{esc(sh.display_name(g[0]['title']))}</b> · {parts_word(len(g))}\n"
                                     "Расходы по валютам и видам — сумма всех частей")
            kb = InlineKeyboardMarkup(inline_keyboard=[[btn("📦 Весь груз", f"g:{gi}")]])
    else:
        text, kb = group_view(cb.from_user.id, gi)
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("q:"))
async def cb_results(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    text, kb = results_view(cb.from_user.id, int(cb.data.split(":")[1]))
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith(("c:", "d:", "v:", "e:")))
async def cb_card(cb: CallbackQuery) -> None:
    rid = await record_cb(cb)
    if rid is None:
        return
    kind, _, ctx = cb.data.split(":", 2)
    app.pending_reason.pop(cb.from_user.id, None)
    rec = app.by_id.get(rid)
    if rec and gtd_day(rec) and app.db.cbu_get(gtd_day(rec)) is None:
        try:
            await ensure_cbu([gtd_day(rec)], 1)
        except Exception:
            pass
    if kind == "d":
        await safe_edit(cb, details_html(rid), card_kb(rid, ctx, cb.from_user.id, details=True))
    elif kind == "e":
        await safe_edit(cb, fit(expenses_html(rid)), card_kb(rid, ctx, cb.from_user.id, exp=True))
    elif kind == "v":
        row, rec = app.db.get(rid), app.by_id.get(rid)
        text = (breakdown_html([rec], f"📦 <b>{esc(row['title'])}</b> · расходы по валютам и видам\n{status_line(row)}")
                if rec else card_html(rid))
        await safe_edit(cb, text, card_kb(rid, ctx, cb.from_user.id, money=True))
    else:
        await safe_edit(cb, card_html(rid), card_kb(rid, ctx, cb.from_user.id))
    await cb.answer()


# ---- кнопки замечаний и поддержки
@dp.callback_query(F.data.startswith("rm:"))
async def cb_remark(cb: CallbackQuery) -> None:
    rid = await record_cb(cb)
    if rid is None:
        return
    if not can_manage(cb.from_user.id):
        await cb.answer("⛔ Замечания оставляет Генеральный Директор", show_alert=True)
        return
    ctx = cb.data.split(":", 2)[2]
    ask_input(cb.from_user.id, "rm", rid, ctx)
    await cb.message.answer(f"📌 Напишите замечание по грузу <b>{esc(app.db.get(rid)['title'])}</b> одним сообщением.\n"
                            "Отмена — /cancel", reply_markup=ForceReply(input_field_placeholder="Замечание…"))
    await cb.answer()


@dp.callback_query(F.data.startswith("ra:"))
async def cb_remark_ack(cb: CallbackQuery) -> None:
    rid = await record_cb(cb)
    if rid is None:
        return
    uid = cb.from_user.id
    if not can_edit(uid):
        await cb.answer("⛔ Наблюдатель не принимает замечания", show_alert=True)
        return
    me = name_of(cb.from_user)
    items = app.db.open_remarks(rid)
    n = app.db.ack_remarks(rid, me)
    if not n:
        await cb.answer("Открытых замечаний нет")
        return
    title = app.db.get(rid)["title"]
    for r in items:
        if r["author_id"] and r["author_id"] != uid:
            await send(r["author_id"], f"👍 <b>Замечание принято</b>\n\n📦 {esc(title)}\n"
                                       f"<blockquote>{esc(r['text'][:300])}</blockquote>👤 {esc(me)}",
                       InlineKeyboardMarkup(inline_keyboard=[[btn("📦 Открыть карточку", f"c:{rid}:-")]]))
    await cb.answer("👍 Принято")
    try:
        await safe_edit(cb, card_html(rid), card_kb(rid, "-", uid))
    except Exception:
        pass


@dp.callback_query(F.data.startswith("sp:"))
async def cb_support(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    uid = cb.from_user.id
    parts = cb.data.split(":")
    act, arg = parts[1], (int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0)
    if not can_edit(uid):
        await cb.answer("⛔ Наблюдатель не пишет в поддержку", show_alert=True)
        return
    staff = uid in support_staff()
    th = app.db.thread_get(arg) if act in ("v", "w", "r", "m", "c") and arg else None
    if act in ("v", "r", "m", "c") and (th is None or not (staff or th["user_id"] == uid)):
        await cb.answer("Обращение не найдено", show_alert=True)
        return
    if act == "l":
        if not staff:
            await cb.answer()
            return
        text, kb = support_view(uid)
        await safe_edit(cb, text, kb)
    elif act == "v":
        await safe_edit(cb, thread_text(arg, uid), thread_kb(arg, uid))
    elif act in ("w", "n"):
        rid = arg if act == "n" else None
        tid = 0
        if act == "w" and th is not None:
            if th["user_id"] != uid and not staff:
                await cb.answer("⛔", show_alert=True)
                return
            tid = th["id"]
        if tid == 0:
            own = app.db.thread_open_of(uid)
            tid = own["id"] if own else 0
        ask_input(uid, "sp", tid, rid)
        about = f" по грузу <b>{esc(app.db.get(rid)['title'])}</b>" if rid and app.db.get(rid) else ""
        await cb.message.answer(f"✍️ Напишите вопрос{about} одним сообщением — он сразу уйдёт Разработчику и "
                                "Директору.\nОтмена — /cancel",
                                reply_markup=ForceReply(input_field_placeholder="Ваш вопрос…"))
    elif act in ("r", "m"):
        if not staff or (act == "m" and not can_manage(uid)):
            await cb.answer("⛔ Недостаточно прав", show_alert=True)
            return
        ask_input(uid, "spr" if act == "r" else "spm", arg)
        what = "ответ" if act == "r" else "замечание"
        await cb.message.answer(f"✍️ Напишите {what} по обращению №{arg} одним сообщением.\nОтмена — /cancel",
                                reply_markup=ForceReply(input_field_placeholder="Текст…"))
    elif act == "c":
        if th["status"] == "open":
            app.db.thread_close(arg)
            me = name_of(cb.from_user)
            for u in ({th["user_id"]} | support_staff()) - {uid}:
                await send(u, f"✅ <b>Обращение №{arg} решено</b>\n👤 закрыл: {esc(me)}",
                           InlineKeyboardMarkup(inline_keyboard=[[btn("💬 Переписка", f"sp:v:{arg}")]]))
        await safe_edit(cb, thread_text(arg, uid), thread_kb(arg, uid))
    await cb.answer()


@dp.callback_query(F.data.startswith("s:"))
async def cb_status(cb: CallbackQuery) -> None:
    rid = await record_cb(cb)
    if rid is None:
        return
    if not can_edit(cb.from_user.id):
        await cb.answer("👁 С вашей ролью можно только смотреть — статусы менять нельзя.", show_alert=True)
        return
    _, _, status_key, ctx = cb.data.split(":", 3)
    status = {"work": WORK, "done": DONE, "force": DONE}[status_key]
    lines, warn = risk_of(app.by_id.get(rid)) if status == DONE else ([], "")
    if status_key == "done" and lines and cfg.CONFIRM_RISKY_CLOSE:
        # Груз с неподтверждёнными / пустыми суммами молча не закрывается — сначала подтверждение
        text = (card_html(rid) + "\n\n⚠️ <b>Закрыть груз, хотя данные не полные?</b>\n" + "\n".join(lines)
                + "\n\nГруз будет отмечен «⚠️ с пометкой» — это увидят Директор, Разработчик и Оператор.")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [btn("✅ Закрыть с пометкой", f"s:{rid}:force:{ctx}")],
            [btn("← Сначала исправлю", f"c:{rid}:{ctx}")]])
        await safe_edit(cb, text, kb)
        await cb.answer("Проверьте перед закрытием", show_alert=False)
        return
    old = app.db.get(rid)["status"]
    app.db.set_status(rid, status, user_id=cb.from_user.id, user_name=cb.from_user.full_name,
                      warn=warn if status == DONE else "")
    app.pending_reason.pop(cb.from_user.id, None)
    await safe_edit(cb, card_html(rid), card_kb(rid, ctx, cb.from_user.id))
    await cb.answer(f"{STATUS_ICON[status]} {STATUS_NAME[status]}" + (" · с пометкой" if warn and status == DONE else ""))
    await notify_owners(rid, cb.from_user.id, old)


@dp.callback_query(F.data.startswith("f:"))
async def cb_fail(cb: CallbackQuery) -> None:
    rid = await record_cb(cb)
    if rid is None:
        return
    if not can_edit(cb.from_user.id):
        await cb.answer("👁 С вашей ролью можно только смотреть — статусы менять нельзя.", show_alert=True)
        return
    ctx = cb.data.split(":", 2)[2]
    app.pending_reason[cb.from_user.id] = (rid, ctx)
    await cb.message.answer(
        f"✍️ Напишите причину, почему «<b>{esc(app.db.get(rid)['title'])}</b>» не завершён:",
        reply_markup=ForceReply(input_field_placeholder="Например: ждём сертификат"))
    await cb.answer()


@dp.callback_query(F.data.startswith("z:"))
async def cb_snooze_menu(cb: CallbackQuery) -> None:
    rid = await record_cb(cb)
    if rid is None:
        return
    ctx = cb.data.split(":", 2)[2]
    try:
        await cb.message.edit_reply_markup(reply_markup=snooze_kb(rid, ctx))
    except TelegramBadRequest:
        pass
    await cb.answer("Когда напомнить?")


def snooze_time(option: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(cfg.TZ)
    if option == "1h":
        return now + timedelta(hours=1)
    if option == "3h":
        return now + timedelta(hours=3)
    when = next_daily_time(now)
    return when + timedelta(days=1) if when.date() == now.date() else when


@dp.callback_query(F.data.startswith("zz:"))
async def cb_snooze(cb: CallbackQuery) -> None:
    rid = await record_cb(cb)
    if rid is None:
        return
    _, _, option, ctx = cb.data.split(":", 3)
    when = snooze_time(option)
    app.db.set_snooze(rid, cb.from_user.id, int(when.timestamp()))
    try:
        await cb.message.edit_reply_markup(reply_markup=card_kb(rid, ctx, cb.from_user.id))
    except TelegramBadRequest:
        pass
    await cb.answer(f"⏰ Напомню {when.strftime('%d.%m в %H:%M')}", show_alert=True)


@dp.callback_query(F.data.startswith(("t:", "tv:")))
async def cb_totals(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    kind, period = cb.data.split(":")[:2]
    await app.refresh()
    records = records_for_period(period)
    if kind == "tv":
        text = breakdown_html(records, f"💱 <b>Расходы по валютам и видам · {esc(PERIODS[period])}</b>\nГрузов: {len(records)}")
        await safe_edit(cb, text, period_kb(period, money=True))
    else:
        await safe_edit(cb, totals_html(records, PERIODS[period]), period_kb(period))
    await cb.answer()


@dp.callback_query(F.data.startswith(("tm:", "tu:")))
async def cb_team(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    if not can_manage(cb.from_user.id):
        await cb.answer("⛔ Раздел только для Генерального Директора", show_alert=True)
        return
    kind, *rest = cb.data.split(":")
    if kind == "tm":
        text, kb = team_view(int(rest[0]))
    else:
        text, kb = team_user_view(int(rest[0]), int(rest[1]), cb.from_user.id)
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("tr:"))
async def cb_team_role(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    actor = cb.from_user.id
    if not can_manage(actor):
        await cb.answer("⛔ Роли назначает Генеральный Директор", show_alert=True)
        return
    _, uid_s, code, page_s = cb.data.split(":")
    uid, page, new = int(uid_s), int(page_s), ROLE_CODE.get(code)
    if new is None:
        await cb.answer("Неизвестная роль", show_alert=True)
        return
    if uid in cfg.OWNER_IDS and new not in ACTIVE_ROLES:
        await cb.answer("Администратору из файла env нельзя закрыть доступ. Можно выбрать любую роль.", show_alert=True)
        return
    if uid == actor and uid not in cfg.OWNER_IDS and new not in ADMIN_ROLES:
        await cb.answer("Свой полный доступ убрать нельзя. Можно выбрать должность: Директор или Разработчик.",
                        show_alert=True)
        return
    current = role_of(uid)
    if current == new:
        await cb.answer("Эта роль уже назначена")
        return
    if current in ADMIN_ROLES and new not in ADMIN_ROLES and uid not in cfg.OWNER_IDS and not (admin_ids() - {uid}):
        await cb.answer("Нельзя убрать последнего администратора", show_alert=True)
        return
    row = app.db.get_user(uid)
    app.db.set_user_role(uid, new, actor, cb.from_user.full_name,
                         name=(row["name"] if row else "") or app.db.subscriber_name(uid))
    extras = app.db.extra_roles(uid)
    if new in extras or (new not in ACTIVE_ROLES and extras):   # основная роль не дублируется, без доступа — без доп. ролей
        app.db.set_extra_roles(uid, [] if new not in ACTIVE_ROLES else [r for r in extras if r != new],
                               actor, cb.from_user.full_name)
    app.pending_reason.pop(uid, None)
    if new in ACTIVE_ROLES:
        app.db.add_subscriber(uid, (row["name"] if row else "") or app.db.subscriber_name(uid))
    else:
        app.db.remove_subscriber(uid)
    text, kb = team_user_view(uid, page, actor)
    await safe_edit(cb, text, kb)
    await cb.answer(f"{ROLE_ICON[new]} {ROLE_SHORT.get(new, ROLE_NAME[new])}")
    if uid != actor:
        await notify_role_change(uid, new)


@dp.callback_query(F.data.startswith("tx:"))
async def cb_team_extra(cb: CallbackQuery) -> None:
    """Дополнительная роль: включить / выключить (только Генеральный Директор)."""
    if not await allowed_cb(cb):
        return
    actor = cb.from_user.id
    if not can_manage(actor):
        await cb.answer("⛔ Роли назначает Генеральный Директор", show_alert=True)
        return
    _, uid_s, code, page_s = cb.data.split(":")
    uid, page, extra = int(uid_s), int(page_s), ROLE_CODE.get(code)
    main = role_of(uid)
    if extra not in EXTRA_ROLES or main is None or extra == main:
        await cb.answer("Эту роль нельзя добавить", show_alert=True)
        return
    cur = app.db.extra_roles(uid)
    if extra in cur:
        new, change, word = [r for r in cur if r != extra], f"-{extra}", "убрана"
    else:
        new, change, word = cur + [extra], f"+{extra}", "добавлена"
    app.db.set_extra_roles(uid, [r for r in EXTRA_ROLES if r in new], actor, cb.from_user.full_name, change,
                           default_role=main, name=app.db.subscriber_name(uid))
    text, kb = team_user_view(uid, page, actor)
    await safe_edit(cb, text, kb)
    await cb.answer(f"{ROLE_ICON[extra]} {ROLE_SHORT.get(extra, ROLE_NAME[extra])}: {word}")
    if uid != actor and is_allowed(uid):
        await send(uid, f"🔑 <b>Ваши роли в боте изменены</b>\n{roles_label(uid)}\n\n"
                        "Нажмите /start, чтобы обновить меню.")


# ===========================================================================
# Новые разделы: Сегодня, Категории, Проверки, Журнал — команды и кнопки
# ===========================================================================
@dp.message(Command("today"))
async def cmd_today(message: Message) -> None:
    async def run():
        await app.refresh()
        text, kb = today_view(message.from_user.id)
        await screen(message, text, kb, "td")
    await guarded(message, run)


@dp.message(Command("cat"))
async def cmd_cats(message: Message) -> None:
    async def run():
        await app.refresh()
        text, kb = cat_root_view("a")
        await screen(message, text, kb, "k:a")
    await guarded(message, run)


@dp.message(Command("checks"))
async def cmd_checks(message: Message) -> None:
    async def run():
        await app.refresh()
        text, kb = checks_root_view()
        await screen(message, text, kb, "x")
    await guarded(message, run)


@dp.message(Command("journal"))
async def cmd_journal(message: Message) -> None:
    async def run():
        if not is_admin(message.from_user.id):
            await message.answer("⛔ Журнал действий доступен только Генеральному Директору и Разработчику.")
            return
        text, kb = journal_view(0)
        await message.answer(text, reply_markup=kb)
    await guarded(message, run)


@dp.callback_query(F.data == "tdi")
async def cb_today_image(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    await app.refresh()
    png = brand.digest_png(digest_data(), user_lang(cb.from_user.id))
    if not png:
        await cb.answer("Нужна библиотека Pillow: py -3.12 -m pip install pillow", show_alert=True)
        return
    await cb.message.answer_photo(BufferedInputFile(png, "uyda-summary.png"),
                                  caption=f"☀️ <b>Сводка на {datetime.now(cfg.TZ).strftime('%d.%m.%Y')}</b>")
    await cb.answer()


@dp.callback_query(F.data == "td")
async def cb_today(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    await app.refresh()
    text, kb = today_view(cb.from_user.id)
    await safe_edit(cb, text, kb)
    await cb.answer("Обновлено")


@dp.callback_query(F.data.startswith(("k:", "kd:", "kc:", "kv:")))
async def cb_categories(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    await app.refresh()
    kind, *rest = cb.data.split(":")
    try:
        if kind == "k":
            text, kb = cat_root_view(rest[0] if rest else "a")
        elif kind == "kd":
            dim, page, per = rest
            text, kb = cat_list_view(dim, int(page), per)
        elif kind == "kc":
            dim, key, page, per = rest
            text, kb = cat_view(cb.from_user.id, dim, key, int(page), per)
        else:
            dim, key, per = rest
            text, kb = cat_breakdown_view(dim, key, per)
    except (ValueError, KeyError):
        text, kb = cat_root_view("a")
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data == "x")
@dp.callback_query(F.data.startswith("x:"))
async def cb_checks(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    await app.refresh()
    parts = cb.data.split(":")
    if len(parts) == 3:
        text, kb = checks_list_view(cb.from_user.id, parts[1], int(parts[2]))
    else:
        text, kb = checks_root_view()
    await safe_edit(cb, text, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith(("j:", "jp:")))
async def cb_journal(cb: CallbackQuery) -> None:
    if not await allowed_cb(cb):
        return
    if not is_admin(cb.from_user.id):
        await cb.answer("⛔ Журнал — только для Директора и Разработчика", show_alert=True)
        return
    kind, value = cb.data.split(":")[:2]
    text, kb = journal_view(int(value)) if kind == "j" else people_view(int(value))
    await safe_edit(cb, text, kb)
    await cb.answer()


# ===========================================================================
# Расписание
# ===========================================================================
def next_daily_time(now: datetime | None = None, at: str | None = None) -> datetime:
    """Ближайшее время `at` («ЧЧ:ММ», по умолчанию время сводки) после `now`."""
    now = now or datetime.now(cfg.TZ)
    hh, mm = (int(x) for x in (at or cfg.DAILY_REMINDER_TIME).split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def next_weekly_time(now: datetime | None = None) -> datetime:
    now = now or datetime.now(cfg.TZ)
    h, m = (int(x) for x in cfg.WEEKLY_REPORT_TIME.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=(cfg.WEEKLY_REPORT_DAY - now.weekday()) % 7)
    return t if t > now else t + timedelta(days=7)


async def scheduler() -> None:
    next_daily = next_daily_time()
    next_sla = next_daily_time(at=cfg.SLA_REMIND_TIME)
    next_week = next_weekly_time()
    next_backup = next_daily_time(at=cfg.BACKUP_TIME)
    last_ping = 0.0
    log.info("Следующая сводка: %s, следующая копия базы: %s",
             next_daily.strftime("%d.%m.%Y %H:%M"), next_backup.strftime("%d.%m.%Y %H:%M"))
    while True:
        await asyncio.sleep(30)
        try:
            await app.refresh()
        except Exception as e:
            # подробный разбор — только при первом сбое, дальше короткая строка (проба идёт каждые 30 секунд)
            if app.watch.down_since and time.time() - app.watch.down_since > 60:
                log.warning("Таблица всё ещё не читается: %s", e)
            else:
                log.exception("Ошибка синхронизации с таблицей")
        try:
            while app.watch_actions:
                await notify_sheet_state(*app.watch_actions.pop(0))
        except Exception:
            log.exception("Не удалось отправить сообщение о состоянии таблицы")
        try:
            if cfg.HEALTHCHECK_URL and not app.watch.is_down and time.time() - last_ping >= cfg.SYNC_INTERVAL_SECONDS:
                last_ping = time.time()
                if not await asyncio.to_thread(rel.ping, cfg.HEALTHCHECK_URL):
                    log.warning("Внешний пульс HEALTHCHECK_URL не ответил")
        except Exception:
            log.exception("Ошибка внешнего пульса")
        try:
            if datetime.now(cfg.TZ) >= next_backup:
                monday = datetime.now(cfg.TZ).weekday() == 0
                next_backup = next_daily_time(at=cfg.BACKUP_TIME)
                await run_backup(send_to_admins=monday)
        except Exception:
            log.exception("Ошибка автокопии базы")
        try:
            for s in app.db.pop_due_snoozes(int(time.time())):
                row = app.db.get(s["record_id"])
                if row and row["status"] in OPEN_STATUSES and is_allowed(s["user_id"]):
                    await send_card(s["user_id"], s["record_id"], "⏰ <b>Напоминание</b>")
        except Exception:
            log.exception("Ошибка отложенных напоминаний")
        try:
            await risk_sync_and_alert()
        except Exception:
            log.exception("Ошибка реестра рисков")
        try:
            if cfg.CBU_ENABLED and app.records:
                await ensure_cbu(cbu_days_wanted(), 4)   # понемногу: 4 даты за 30 секунд
        except Exception:
            log.exception("Ошибка загрузки курса ЦБ")
        try:
            if datetime.now(cfg.TZ) >= next_sla:
                next_sla = next_daily_time(at=cfg.SLA_REMIND_TIME)
                if datetime.now(cfg.TZ).weekday() != 6:     # в воскресенье не напоминаем
                    await sla_reminders()
        except Exception:
            log.exception("Ошибка напоминаний о сроках этапов")
        try:
            if datetime.now(cfg.TZ) >= next_week:
                next_week = next_weekly_time()
                await send_weekly(manager_ids())
                log.info("Недельный отчёт отправлен")
        except Exception:
            log.exception("Ошибка недельного отчёта")
        if datetime.now(cfg.TZ) >= next_daily:
            next_daily = next_daily_time()
            try:
                text, kb = digest_html()
                data = digest_data()
                for u in recipients():
                    png = brand.digest_png(data, user_lang(u))
                    if png:
                        try:
                            await app.bot.send_photo(u, BufferedInputFile(png, "uyda-summary.png"))
                        except Exception:
                            log.exception("Не удалось отправить картинку-сводку %s", u)
                    await send(u, text, kb)
                log.info("Сводка отправлена, следующая: %s", next_daily.strftime("%d.%m.%Y %H:%M"))
            except Exception:
                log.exception("Ошибка ежедневной сводки")


async def setup_profile(bot: Bot) -> None:
    """Описание бота и меню команд в Telegram."""
    try:
        await bot.set_my_commands([
            BotCommand(command="today", description="🏠 Сегодня: что горит"),
            BotCommand(command="open", description="📋 Открытые работы"),
            BotCommand(command="cat", description="🗂 Категории: страны, поставщики, транспорт"),
            BotCommand(command="checks", description="🧪 Проверки данных таблицы"),
            BotCommand(command="done", description="✅ Завершённые по месяцам"),
            BotCommand(command="itogo", description="📊 Итоги за период"),
            BotCommand(command="gtd", description="🧾 Поиск по номеру ГТД"),
            BotCommand(command="auto", description="🚛 Поиск по номеру машины"),
            BotCommand(command="kkdg", description="📑 Поиск по номеру ККДГ"),
            BotCommand(command="logist", description="🚚 Грузы перевозчика"),
            BotCommand(command="otpr", description="🏭 Грузы отправителя"),
            BotCommand(command="docs", description="📎 Документы: обмен файлами по партиям"),
            BotCommand(command="dash", description="📊 Дашборд: графики и таблицы"),
            BotCommand(command="support", description="🆘 Поддержка: вопрос Разработчику"),
            BotCommand(command="remarks", description="📌 Замечания Генерального Директора"),
            BotCommand(command="reload", description="🔄 Перечитать таблицу"),
            BotCommand(command="journal", description="🗒 Журнал действий (Директор, Разработчик)"),
            BotCommand(command="adduser", description="➕ Дать доступ по ID (Генеральный Директор)"),
            BotCommand(command="status", description="🩺 Состояние бота (для администраторов)"),
            BotCommand(command="help", description="📖 Справочник"),
        ])
        await bot.set_my_description(BOT_DESCRIPTION)
        await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
        cmds = await bot.get_my_commands()
        cmds.append(BotCommand(command="lang", description="🌐 Язык"))
        await bot.set_my_commands(cmds)
        for lang, code in (("uz", "uz"), ("en", "en")):   # меню команд на языке телефона пользователя
            await bot.set_my_commands([BotCommand(command=c.command, description=i18n.tr(c.description, lang)[:256])
                                       for c in cmds], language_code=code)
            await bot.set_my_description(i18n.tr(BOT_DESCRIPTION, lang), language_code=code)
            await bot.set_my_short_description(i18n.tr(BOT_SHORT_DESCRIPTION, lang), language_code=code)
    except Exception:
        log.warning("Не удалось обновить описание бота — продолжаю без него")


def ask_token() -> str:
    """Токена нет или он записан неправильно — спросить в окне и сохранить в env."""
    import os
    path = cfg.ENV_FILE or os.path.join(cfg.BASE_DIR, "env")
    print("\n" + "=" * 70)
    print("❗ В файле настроек нет правильного токена бота.")
    print(f"   Файл: {path}")
    print("   Токен выглядит так: 1234567890:AAH...  (цифры, двоеточие, длинная строка)")
    print("   Где взять: Telegram → @BotFather → /mybots → @Uyda_import_bot → API Token → скопировать.")
    print("=" * 70)
    for _ in range(3):
        raw = input("\nВставьте токен (правая кнопка мыши) и нажмите Enter: ")
        token = cfg.clean_token(raw)
        if cfg.token_ok(token):
            break
        print("Это не похоже на токен. Нужен вид 1234567890:AAH...  Попробуйте ещё раз.")
    else:
        raise SystemExit("Токен не введён. Запустите start.bat ещё раз.")
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    done = False
    for i, line in enumerate(lines):
        if line.strip().startswith("TELEGRAM_BOT_TOKEN"):
            lines[i] = f"TELEGRAM_BOT_TOKEN={token}"
            done = True
    if not done:
        lines.insert(0, f"TELEGRAM_BOT_TOKEN={token}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"✅ Токен сохранён в {path}. Больше вводить не нужно.\n")
    return token


async def main() -> None:
    global app
    if not cfg.token_ok(cfg.TELEGRAM_TOKEN):
        cfg.TELEGRAM_TOKEN = ask_token()
    if not cfg.EXCEL_FILE and not cfg.SPREADSHEET_ID:
        raise SystemExit("Не заполнен GOOGLE_SPREADSHEET_ID (или EXCEL_FILE для тестового режима).")
    if cfg.EXCEL_FILE:
        import os
        if not os.path.exists(cfg.EXCEL_FILE):
            raise SystemExit(f"Не найден Excel-файл «{cfg.EXCEL_FILE}» — положите его в папку с ботом.")
        log.info("ТЕСТОВЫЙ РЕЖИМ: данные из Excel-файла %s", cfg.EXCEL_FILE)
    if not cfg.ANTHROPIC_API_KEY:
        log.info("Ключ Claude не указан — бесплатный режим (поиск отвечает карточками)")
    if not cfg.OWNER_IDS:
        log.warning("OWNER_IDS пуст — некому выдавать роли кнопками, а запросы доступа никому не придут. "
                    "Узнайте свой ID командой /id и впишите его в OWNER_IDS файла env.")

    app = App()
    app.bot = Bot(cfg.TELEGRAM_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await app.bot.get_me()
        log.info("Подключён к Telegram как @%s", me.username)
    except TelegramUnauthorizedError:
        await app.bot.session.close()
        raise SystemExit("\n❌ Telegram не принял токен: он неверный или его отозвали в @BotFather.\n"
                         "Возьмите актуальный: @BotFather → /mybots → @Uyda_import_bot → API Token,\n"
                         f"вставьте в строку TELEGRAM_BOT_TOKEN= в файле {cfg.ENV_FILE or 'env'} и запустите снова.")
    except Exception as e:
        log.warning("Не удалось проверить связь с Telegram (%s) — пробую дальше", type(e).__name__)
    log.info("Языки: русский, o‘zbekcha, English — фраз в словаре: %d", i18n.load())
    app.bot.session.middleware(TranslateOutgoing())
    records = await app.refresh(force=True)
    log.info("Таблица: %d грузов, открытых работ: %d", len(records), len(app.db.open_records()))
    await setup_profile(app.bot)
    last_backup = app.db.get_meta("last_backup")
    if not last_backup or time.time() - int(last_backup) > 20 * 3600:
        await run_backup()   # при запуске — копия, если последней больше 20 часов
    asyncio.create_task(scheduler())
    log.info("Бот запущен")
    if cfg.STARTUP_NOTICE:
        src, stale = source_info()
        await notify_admins(f"🟢 <b>Бот запущен</b> · {datetime.now(cfg.TZ).strftime('%d.%m.%Y %H:%M')}\n"
                            f"📋 Грузов: {len(records)} · открытых работ: {len(app.db.open_records())}\n"
                            f"📄 {src}" + ("\n⚠️ Файл давно не обновлялся." if stale else ""))
    await dp.start_polling(app.bot)


if __name__ == "__main__":
    asyncio.run(main())
