"""
Три языка бота: русский (основной, на нём написан код), o‘zbekcha (латиница) и English.

Как работает: тексты бота написаны по-русски; перед отправкой в Telegram каждое сообщение, подпись и кнопка
переводятся на язык получателя по словарю i18n_catalog_*.txt (фразы целиком, длинные — первыми).
Нажатая кнопка меню на узбекском / английском переводится обратно на русский, чтобы её понял обработчик.
Если фразы нет в словаре, она остаётся по-русски — бот не ломается.
"""

import contextvars
import glob
import os
import re

LANGS = {"ru": "🇷🇺 Русский", "uz": "🇺🇿 O‘zbekcha", "en": "🇬🇧 English"}
DEFAULT = "ru"
CURRENT: contextvars.ContextVar[str] = contextvars.ContextVar("lang", default=DEFAULT)

_CYR = "А-Яа-яЁё"
_cat: dict[str, dict[str, str]] = {"uz": {}, "en": {}}
_rx: dict[str, re.Pattern | None] = {"uz": None, "en": None}
_reverse: dict[str, str] = {}          # «🏠 Bugun» -> «🏠 Сегодня» (кнопки меню)

# Фразы с числами (окончания зависят от числа) — правила до словаря
_RULES: list[tuple[re.Pattern, dict[str, object]]] = [
    (re.compile(r"(\d+) дн\."), {"uz": r"\1 kun", "en": r"\1 d"}),
    (re.compile(r"(\d+) (?:часть|части|частей)"), {"uz": r"\1 qism", "en": lambda m: f"{m[1]} part" + ("" if m[1] == "1" else "s")}),
    (re.compile(r"(\d+) мин\."), {"uz": r"\1 daq.", "en": r"\1 min"}),
    (re.compile(r"(\d+) ч\."), {"uz": r"\1 soat", "en": r"\1 h"}),
    (re.compile(r"(\d\d\.\d\d) в (\d\d:\d\d)"), {"uz": r"\1 soat \2", "en": r"\1 at \2"}),
    (re.compile(r"(\d+) гр\."), {"uz": r"\1 ta yuk", "en": r"\1 shp."}),
    (re.compile(r"(\d+) (?:груз|груза|грузов|партия|партии|партий)(?![а-я])"), {"uz": r"\1 ta yuk",
                                                        "en": lambda m: f"{m[1]} shipment" + ("" if m[1] == "1" else "s")}),
    (re.compile(r"(\d+) (?:файл|файла|файлов)\.?(?![а-я])"), {"uz": r"\1 ta fayl",
                                                   "en": lambda m: f"{m[1]} file" + ("" if m[1] == "1" else "s")}),
    (re.compile(r"(\d+) стр\."), {"uz": r"\1 qator", "en": r"\1 rows"}),
    (re.compile(r"\(по (\d+)-е\)"), {"uz": r"(\1-sanagacha)", "en": r"(to day \1)"}),
    (re.compile(r"к (\d+)-му числу"), {"uz": r"\1-sanagacha", "en": r"by day \1"}),
    (re.compile(r"(\d+) дней"), {"uz": r"\1 kun", "en": r"\1 days"}),
]


def _unescape(s: str) -> str:
    return s.replace("\\n", "\n")


def load(folder: str | None = None) -> int:
    """Читает словари i18n_catalog_*.txt из папки бота. Возвращает число фраз."""
    folder = folder or os.path.dirname(os.path.abspath(__file__))
    for lang in _cat:
        _cat[lang].clear()
    _reverse.clear()
    for path in sorted(glob.glob(os.path.join(folder, "i18n_catalog_*.txt"))):
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                parts = line.split(" ¦ ")
                if len(parts) != 3:
                    continue
                ru, uz, en = (_unescape(p).strip() for p in parts)
                if not ru:
                    continue
                for lang, val in (("uz", uz), ("en", en)):
                    if val:
                        _cat[lang][ru] = val
                        _reverse.setdefault(val, ru)
                        alt = ru_terms(ru)            # тот же текст, уже с «партия» вместо «груз»
                        if alt != ru:
                            _cat[lang].setdefault(alt, val)
    for lang in _cat:
        _rx[lang] = _compile(_cat[lang])
    return len(_cat["uz"])


def _compile(cat: dict[str, str]) -> re.Pattern | None:
    if not cat:
        return None
    parts = []
    for key in sorted(cat, key=len, reverse=True):
        p = re.escape(key)
        if re.match(f"[{_CYR}A-Za-z0-9]", key):
            p = f"(?<![{_CYR}A-Za-z0-9])" + p
        if re.search(f"[{_CYR}A-Za-z0-9]$", key):
            p = p + f"(?![{_CYR}A-Za-z0-9])"
        parts.append(p)
    return re.compile("|".join(parts))


# Русские тексты: «груз» показываем как «партия» (товара), чтобы не путать с «графой».
# Сначала фразы, где меняется род прилагательного или глагола, потом сами формы слова.
_RU_PHRASES = [
    ("Новый груз", "Новая партия"), ("новый груз", "новая партия"), ("Весь груз", "Вся партия"),
    ("весь груз", "вся партия"), ("ВСЕМУ грузу", "ВСЕЙ партии"), ("открытого груза", "открытой партии"),
    ("одного груза", "одной партии"), ("того же груза", "той же партии"), ("всего груза", "всей партии"),
    ("конкретному грузу", "конкретной партии"), ("завершённом грузе", "завершённой партии"),
    ("одном грузе", "одной партии"), ("один груз", "одна партия"), ("Такой груз", "Такая партия"),
    ("такой груз", "такая партия"), ("соседний груз", "соседняя партия"), ("Пред. груз", "Пред. партия"),
    ("След. груз", "След. партия"), ("Груз вернулся", "Партия вернулась"), ("Груз передан", "Партия передана"),
    ("груз передан", "партия передана"), ("Груз открыт", "Партия открыта"), ("Груз будет отмечен", "Партия будет отмечена"),
    ("Груз с таким номером не найден", "Партия с таким номером не найдена"), ("Груз не найден", "Партия не найдена"),
    ("груз появился", "партия появилась"), ("груз не завершён", "партия не завершена"),
    ("груз сам переходит", "партия сама переходит"), ("груз завершён", "партия завершена"),
    # статусы относятся к партии — женский род
    ("Не завершён", "Не завершена"), ("не завершён", "не завершена"), ("Завершён", "Завершена"),
    ("завершён", "завершена"), ("Закрыт с пометкой", "Закрыта с пометкой"), ("закрыт с пометкой", "закрыта с пометкой"),
]
_RU_WORDS = {"груз": "партия", "груза": "партии", "грузу": "партии", "грузом": "партией", "грузе": "партии",
             "грузы": "партии", "грузов": "партий", "грузам": "партиям", "грузами": "партиями", "грузах": "партиях"}
_RU_ACC = re.compile(r"(?i)(?<![\w])(на|за|про|в|Откройте|откройте|Закрыть|закрыть|завершить|Завершить|взять)\s+груз(?![\w])")
_RU_WORD = re.compile(r"(?<![А-Яа-яЁёA-Za-z])([Гг])руз(а|у|ом|е|ы|ов|ам|ами|ах)?(?![А-Яа-яЁёA-Za-z])")


def ru_terms(text: str) -> str:
    if not text or "руз" not in text:
        return text
    for a, b in _RU_PHRASES:
        text = text.replace(a, b)
    text = _RU_ACC.sub(lambda m: f"{m.group(1)} партию", text)

    def word(m):
        new = _RU_WORDS["груз" + (m.group(2) or "")]
        return new.capitalize() if m.group(1) == "Г" else new
    return _RU_WORD.sub(word, text)


def tr(text: str | None, lang: str | None = None) -> str | None:
    """Перевод готового текста на язык lang (по умолчанию — язык текущего пользователя).
    Для русского — только замена «груз» → «партия»."""
    lang = lang or CURRENT.get()
    if lang == "ru" or lang not in _cat:
        return ru_terms(text) if isinstance(text, str) else text
    if not text or not re.search(f"[{_CYR}]", text):
        return text
    rx = _rx.get(lang)
    if rx is not None:                       # сначала словарь (в нём есть фразы с числами: «за 7 дней»)
        cat = _cat[lang]
        text = rx.sub(lambda m: cat[m.group(0)], text)
    for rx, repl in _RULES:                  # потом правила для остальных чисел
        r = repl.get(lang)
        if r is not None:
            text = rx.sub(r, text)
    return text


def back_to_ru(text: str | None) -> str | None:
    """Нажатая кнопка меню на другом языке -> исходный русский текст (для обработчиков)."""
    if not text:
        return text
    return _reverse.get(text.strip(), text)


def detect(language_code: str | None) -> str:
    """Язык по настройке Telegram у пользователя (при первом входе)."""
    code = (language_code or "").lower()
    if code.startswith("uz"):
        return "uz"
    if code.startswith("ru") or not code:
        return "ru"
    return "en"
