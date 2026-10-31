"""
Фирменные картинки бота в стиле Uyda: приветствие, ежедневная сводка, картинка описания бота.
Цвет бренда — оранжевый #FF4900, логотип — белый на оранжевом. Шрифт Inter (лицензия OFL, в папке brand/fonts).
Нужна библиотека Pillow (py -3.12 -m pip install pillow). Если её нет — бот работает как раньше, только без картинок.
"""

import io
import os
from datetime import datetime

import i18n

try:
    from PIL import Image, ImageDraw, ImageFont
    AVAILABLE = True
except ImportError:          # нет Pillow — картинки отключены
    AVAILABLE = False

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brand")
ORANGE = (255, 73, 0)
ORANGE_SOFT = (255, 214, 196)
ORANGE_TINT = (255, 241, 234)
INK = (31, 26, 23)
MUTED = (122, 112, 105)
BG = (250, 247, 245)
CARD = (255, 255, 255)
LINE = (236, 228, 222)
TEAL = (11, 122, 102)
TEAL_TINT = (221, 242, 237)
RED = (196, 43, 28)
AMBER = (196, 132, 0)
WHITE = (255, 255, 255)
SLOGAN = "Shinam • Qulay • Sifatli"

_fonts: dict = {}


def font(weight: str, size: int):
    key = (weight, size)
    if key not in _fonts:
        path = os.path.join(HERE, "fonts", f"Inter-{weight}.otf")
        try:
            _fonts[key] = ImageFont.truetype(path, size)
        except OSError:
            _fonts[key] = ImageFont.load_default(size)
    return _fonts[key]


def _logo(height: int, color: str = "white"):
    im = Image.open(os.path.join(HERE, f"logo_{color}.png")).convert("RGBA")
    w = round(im.width * height / im.height)
    return im.resize((w, height), Image.LANCZOS)


def _png(im) -> bytes:
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _fit(draw, text: str, f, width: int) -> str:
    """Обрезать строку с «…», чтобы влезла в ширину."""
    if draw.textlength(text, font=f) <= width:
        return text
    while text and draw.textlength(text + "…", font=f) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(draw, text: str, f, width: int, max_lines: int = 3) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = f"{cur} {w}".strip()
        if draw.textlength(t, font=f) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _fit(draw, lines[-1] + " …", f, width)
    return lines


def _pill(draw, xy, text: str, f, fg, bg, pad=(18, 9)):
    x, y = xy
    w = draw.textlength(text, font=f)
    h = f.size + pad[1] * 2
    draw.rounded_rectangle((x, y, x + w + pad[0] * 2, y + h), radius=h // 2, fill=bg)
    draw.text((x + pad[0], y + pad[1] - 2), text, font=f, fill=fg)
    return x + w + pad[0] * 2


# ---------------------------------------------------------------------------
# Приветствие (/start)
# ---------------------------------------------------------------------------
def welcome_png(name: str, role: str, lang: str) -> bytes | None:
    if not AVAILABLE:
        return None
    t = lambda s: i18n.tr(s, lang)
    W, H = 1280, 640
    im = Image.new("RGB", (W, H), ORANGE)
    d = ImageDraw.Draw(im)
    logo = _logo(112)
    im.paste(logo, (80, 72), logo)
    hello = {"ru": "Здравствуйте", "uz": "Assalomu alaykum", "en": "Hello"}[lang]
    size = 70
    while size > 44 and d.textlength(f"{hello}, {name}!", font=font("ExtraBold", size)) > W - 160:
        size -= 2                                   # длинное имя — шрифт поменьше
    head = _fit(d, f"{hello}, {name}!", font("ExtraBold", size), W - 160)
    d.text((80, 262 + (70 - size) // 2), head, font=font("ExtraBold", size), fill=WHITE)
    sub = t("Импортные грузы GOLD DISHES / UYDA: статусы, расходы, себестоимость, категории.")
    y = 360
    for line in _wrap(d, sub, font("Medium", 34), W - 160, 2):
        d.text((80, y), line, font=font("Medium", 34), fill=WHITE)
        y += 46
    _pill(d, (80, y + 26), t(role), font("SemiBold", 30), ORANGE, WHITE, pad=(24, 12))
    d.text((80, H - 74), SLOGAN, font=font("SemiBold", 28), fill=ORANGE_SOFT)
    langs = "RU · UZ · EN"
    d.text((W - 80 - d.textlength(langs, font=font("SemiBold", 28)), H - 74), langs,
           font=font("SemiBold", 28), fill=ORANGE_SOFT)
    return _png(im)


# ---------------------------------------------------------------------------
# Ежедневная сводка
# ---------------------------------------------------------------------------
MONTHS_SHORT = {
    "ru": ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"],
    "uz": ["yan", "fev", "mar", "apr", "may", "iyn", "iyl", "avg", "sen", "okt", "noy", "dek"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}
TITLES = {
    "summary": {"ru": "Сводка дня", "uz": "Kunlik hisobot", "en": "Daily summary"},
    "attention": {"ru": "Требуют внимания", "uz": "E’tibor talab qiladi", "en": "Need attention"},
    "months": {"ru": "Грузы по месяцам · ГТД ИМ40", "uz": "Oylar bo‘yicha yuklar · IM40 BYD",
               "en": "Shipments by month · IM40 GTD"},
    "calm": {"ru": "Открытых работ нет", "uz": "Ochiq ishlar yo‘q", "en": "No open tasks"},
    "days": {"ru": "дн.", "uz": "kun", "en": "d"},
    "dept": {"ru": "GOLD DISHES · отдел закупок", "uz": "GOLD DISHES · xarid bo‘limi",
             "en": "GOLD DISHES · procurement"},
}


def digest_png(data: dict, lang: str) -> bytes | None:
    """data: date (datetime), kpi [(подпись, число, цвет-код)], attention [(груз, дней, 'customs'|'operator'|'')],
    months [(год, месяц, грузов)]. Подписи — по-русски, переводятся здесь."""
    if not AVAILABLE:
        return None
    t = lambda s: i18n.tr(s, lang)
    W, H = 1080, 1600
    PAD = 56
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    # шапка
    d.rectangle((0, 0, W, 268), fill=ORANGE)
    logo = _logo(78)
    im.paste(logo, (PAD, 58), logo)
    date = data["date"].strftime("%d.%m.%Y")
    f_date = font("ExtraBold", 58)
    d.text((W - PAD - d.textlength(date, font=f_date), 52), date, font=f_date, fill=WHITE)
    lab = TITLES["summary"][lang]
    d.text((W - PAD - d.textlength(lab, font=font("SemiBold", 30)), 124), lab, font=font("SemiBold", 30), fill=WHITE)
    d.text((PAD, 196), TITLES["dept"][lang], font=font("Medium", 26), fill=ORANGE_SOFT)
    d.text((W - PAD - d.textlength(SLOGAN, font=font("Medium", 26)), 196), SLOGAN, font=font("Medium", 26), fill=ORANGE_SOFT)

    # показатели: 3 × 2
    colors = {"ink": INK, "orange": ORANGE, "teal": TEAL, "red": RED, "amber": AMBER, "muted": MUTED}
    kpi = data["kpi"][:6]
    cw, ch, gap, top = (W - PAD * 2 - 2 * 20) // 3, 178, 20, 300
    for i, (label, value, color) in enumerate(kpi):
        x = PAD + (i % 3) * (cw + gap)
        y = top + (i // 3) * (ch + gap)
        d.rounded_rectangle((x, y, x + cw, y + ch), radius=22, fill=CARD, outline=LINE, width=2)
        d.text((x + 26, y + 22), str(value), font=font("ExtraBold", 76), fill=colors.get(color, INK))
        for j, line in enumerate(_wrap(d, t(label), font("Medium", 26), cw - 52, 2)):
            d.text((x + 26, y + 112 + j * 30), line, font=font("Medium", 26), fill=MUTED)

    # требуют внимания
    y = top + 2 * (ch + gap) + 22
    d.text((PAD, y), TITLES["attention"][lang], font=font("Bold", 34), fill=INK)
    y += 58
    rows = data["attention"][:5]
    box_h = max(1, len(rows)) * 70 + 16
    d.rounded_rectangle((PAD, y, W - PAD, y + box_h), radius=22, fill=CARD, outline=LINE, width=2)
    if not rows:
        d.text((PAD + 28, y + 26), TITLES["calm"][lang], font=font("Medium", 30), fill=MUTED)
    resp_style = {"customs": (t("Таможня"), ORANGE, ORANGE_TINT), "operator": ("1C UNF", TEAL, TEAL_TINT)}
    for i, (title, days, resp) in enumerate(rows):
        ry = y + 8 + i * 70
        if i:
            d.line((PAD + 24, ry, W - PAD - 24, ry), fill=LINE, width=2)
        right = W - PAD - 26
        if resp in resp_style:
            txt, fg, bg = resp_style[resp]
            pw = d.textlength(txt, font=font("SemiBold", 24)) + 36
            _pill(d, (right - pw, ry + 15), txt, font("SemiBold", 24), fg, bg, pad=(18, 8))
            right -= pw + 20
        dtxt = f"{days} {TITLES['days'][lang]}" if days > 0 else t("сегодня")
        d.text((right - d.textlength(dtxt, font=font("SemiBold", 28)), ry + 18), dtxt, font=font("SemiBold", 28),
               fill=RED if days >= data.get("overdue", 7) else MUTED)
        right -= d.textlength(dtxt, font=font("SemiBold", 28)) + 24
        d.text((PAD + 28, ry + 17), _fit(d, title, font("SemiBold", 30), right - PAD - 40), font=font("SemiBold", 30),
               fill=INK)

    # грузы по месяцам
    y += box_h + 34
    d.text((PAD, y), TITLES["months"][lang], font=font("Bold", 34), fill=INK)
    y += 60
    months = data["months"][-12:]
    ch_top, ch_bot = y + 30, H - 120
    mx = max([m[2] for m in months] + [1])
    bw = (W - PAD * 2) / max(1, len(months))
    for k in (0.5, 1.0):
        gy = ch_bot - (ch_bot - ch_top) * k
        d.line((PAD, gy, W - PAD, gy), fill=LINE, width=2)
    d.line((PAD, ch_bot, W - PAD, ch_bot), fill=LINE, width=2)
    for i, (yy, mm, n) in enumerate(months):
        x0 = PAD + i * bw + bw * 0.2
        x1 = PAD + (i + 1) * bw - bw * 0.2
        h = (ch_bot - ch_top) * n / mx
        last = i == len(months) - 1
        if n and h >= 2:
            d.rounded_rectangle((x0, ch_bot - h, x1, ch_bot), radius=min(8, int(h // 2)), fill=ORANGE if last else ORANGE_SOFT)
            val = str(n)
            d.text(((x0 + x1) / 2 - d.textlength(val, font=font("SemiBold", 22)) / 2, ch_bot - h - 30), val,
                   font=font("SemiBold", 22), fill=INK if last else MUTED)
        lab = MONTHS_SHORT[lang][mm - 1]
        d.text(((x0 + x1) / 2 - d.textlength(lab, font=font("Medium", 22)) / 2, ch_bot + 10), lab,
               font=font("Medium", 22), fill=INK if last else MUTED)

    # подвал
    d.line((PAD, H - 66, W - PAD, H - 66), fill=LINE, width=2)
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    d.text((PAD, H - 50), "@Uyda_import_bot", font=font("SemiBold", 24), fill=ORANGE)
    d.text((W - PAD - d.textlength(stamp, font=font("Medium", 24)), H - 50), stamp, font=font("Medium", 24), fill=MUTED)
    return _png(im)


# ---------------------------------------------------------------------------
# Картинка описания бота (@BotFather → Edit Description Picture, 640×360)
# ---------------------------------------------------------------------------
def description_png() -> bytes | None:
    if not AVAILABLE:
        return None
    W, H = 640, 360
    im = Image.new("RGB", (W, H), ORANGE)
    d = ImageDraw.Draw(im)
    logo = _logo(96)
    im.paste(logo, ((W - logo.width) // 2, 86), logo)
    line = "Import yuklari · Импорт · Imports"
    d.text(((W - d.textlength(line, font=font("SemiBold", 26))) / 2, 214), line, font=font("SemiBold", 26), fill=WHITE)
    d.text(((W - d.textlength(SLOGAN, font=font("Medium", 22))) / 2, 266), SLOGAN, font=font("Medium", 22),
           fill=ORANGE_SOFT)
    return _png(im)


# ---------------------------------------------------------------------------
# Карточка груза картинкой
# ---------------------------------------------------------------------------
CARD_T = {
    "factory": {"ru": "Цена завода", "uz": "Zavod narxi", "en": "Factory price"},
    "costs": {"ru": "Расходы", "uz": "Xarajatlar", "en": "Costs"},
    "landed": {"ru": "Себестоимость", "uz": "Tannarx", "en": "Landed cost"},
    "by_type": {"ru": "Расходы по видам", "uz": "Xarajat turlari", "en": "Costs by type"},
    "state": {"ru": "Что осталось", "uz": "Nima qoldi", "en": "What is left"},
    "all_filled": {"ru": "Все расходы заполнены", "uz": "Barcha xarajatlar to‘ldirilgan",
                   "en": "All costs are filled"},
    "missing": {"ru": "Не заполнено", "uz": "To‘ldirilmagan", "en": "Not filled"},
    "yellow": {"ru": "Не подтверждено (жёлтые ячейки)", "uz": "Tasdiqlanmagan (sariq kataklar)",
               "en": "Unconfirmed (yellow cells)"},
    "prelim": {"ru": "предварительно", "uz": "taxminiy", "en": "preliminary"},
    "row": {"ru": "Строка", "uz": "Qator", "en": "Row"},
    "no_costs": {"ru": "Расходов пока нет", "uz": "Hozircha xarajat yo‘q", "en": "No costs yet"},
}


def _usd(v) -> str:
    return "—" if v is None else "$" + f"{v:,.0f}".replace(",", " ")


def card_png(c: dict, lang: str) -> bytes | None:
    if not AVAILABLE or not c:
        return None
    t = lambda s: i18n.tr(s, lang)
    T = lambda k: CARD_T[k][lang]
    W, H = 1080, 1350
    PAD = 56
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    # шапка
    d.rectangle((0, 0, W, 330), fill=ORANGE)
    logo = _logo(54)
    im.paste(logo, (PAD, 44), logo)
    st = t(c["status"])
    stf = font("SemiBold", 26)
    sw = d.textlength(st, font=stf) + 44
    _pill(d, (W - PAD - sw, 48), st, stf, ORANGE, WHITE, pad=(22, 10))
    y = 134
    for line in _wrap(d, c["title"], font("ExtraBold", 56), W - PAD * 2, 2):
        d.text((PAD, y), line, font=font("ExtraBold", 56), fill=WHITE)
        y += 66
    sub = " · ".join(x for x in (t(c["regime"].split(" · ")[0]), t(c["country"]).split(" ", 1)[-1],
                                 t(c["transport"]).split(" ", 1)[-1]) if x)
    d.text((PAD, max(y + 8, 262)), _fit(d, sub, font("Medium", 28), W - PAD * 2), font=font("Medium", 28),
           fill=ORANGE_SOFT)

    # деньги: 3 плитки
    top = 362
    cw, ch, gap = (W - PAD * 2 - 40) // 3, 168, 20
    prelim = c.get("prelim", bool(c["missing"] or c["unconfirmed"]))
    tiles = [(T("factory"), _usd(c["factory"] or None), INK, ""),
             (T("costs"), _usd(c["overhead"]), INK, f"{c['pct']:.0f}%" if c.get("pct") is not None else ""),
             (T("landed"), _usd(c["landed"]), ORANGE, T("prelim") if prelim and c["landed"] else "")]
    for i, (lab, val, color, note) in enumerate(tiles):
        x = PAD + i * (cw + gap)
        d.rounded_rectangle((x, top, x + cw, top + ch), radius=22, fill=CARD, outline=LINE, width=2)
        d.text((x + 24, top + 22), lab, font=font("Medium", 24), fill=MUTED)
        size = 46
        while size > 30 and d.textlength(val, font=font("ExtraBold", size)) > cw - 48:
            size -= 2
        d.text((x + 24, top + 60), val, font=font("ExtraBold", size), fill=color)
        if note:
            d.text((x + 24, top + 122), note, font=font("SemiBold", 22), fill=AMBER if note == T("prelim") else MUTED)

    # расходы по видам — полосы
    y = top + ch + 40
    d.text((PAD, y), T("by_type"), font=font("Bold", 32), fill=INK)
    y += 54
    types = c["types"]
    if not types:
        d.text((PAD, y), T("no_costs"), font=font("Medium", 28), fill=MUTED)
        y += 50
    mx = max([v for _, v in types] + [1])
    total = sum(v for _, v in types) or 1
    label_w, val_w = 360, 190
    for lab, v in types:
        lab = _fit(d, t(lab), font("Medium", 26), label_w - 10)
        d.text((PAD, y), lab, font=font("Medium", 26), fill=INK)
        bx0, bx1 = PAD + label_w, W - PAD - val_w
        d.rounded_rectangle((bx0, y + 8, bx1, y + 24), radius=8, fill=ORANGE_TINT)
        d.rounded_rectangle((bx0, y + 8, bx0 + max(16, (bx1 - bx0) * v / mx), y + 24), radius=8, fill=ORANGE)
        val = f"{_usd(v)} · {v / total * 100:.0f}%"
        d.text((W - PAD - d.textlength(val, font=font("SemiBold", 24)), y + 2), val, font=font("SemiBold", 24), fill=INK)
        y += 48

    # что осталось
    y += 22
    d.text((PAD, y), T("state"), font=font("Bold", 32), fill=INK)
    resp = c.get("resp")
    if resp in ("customs", "operator"):
        txt, fg, bg = ((t("Специалист по таможенному оформлению"), ORANGE, ORANGE_TINT) if resp == "customs"
                       else (t("1С УНФ-Оператор"), TEAL, TEAL_TINT))
        pw = d.textlength(txt, font=font("SemiBold", 24)) + 36
        _pill(d, (W - PAD - pw, y + 2), txt, font("SemiBold", 24), fg, bg, pad=(18, 8))
    y += 58
    box_top = y
    lines = []
    if c["missing"]:
        lines.append((f"{T('missing')}: {len(c['missing'])}", RED, "Bold"))
        lines += [("•  " + t(m), INK, "Medium") for m in c["missing"][:5]]
        if len(c["missing"]) > 5:
            lines.append((f"…  +{len(c['missing']) - 5}", MUTED, "Medium"))
    else:
        lines.append((T("all_filled"), TEAL, "Bold"))
    if c["unconfirmed"]:
        lines.append((f"{T('yellow')}: {c['unconfirmed']}", AMBER, "Bold"))
    box_h = len(lines) * 42 + 36
    d.rounded_rectangle((PAD, box_top, W - PAD, box_top + box_h), radius=22, fill=CARD, outline=LINE, width=2)
    for i, (txt, color, w) in enumerate(lines):
        d.text((PAD + 28, box_top + 20 + i * 42), _fit(d, txt, font(w, 27), W - PAD * 2 - 56), font=font(w, 27),
               fill=color)

    # подвал
    d.line((PAD, H - 96, W - PAD, H - 96), fill=LINE, width=2)
    left = " · ".join(x for x in (c["gtd"], f"{T('row')} {c['row']}") if x)
    d.text((PAD, H - 80), _fit(d, left, font("Medium", 24), W - PAD * 2), font=font("Medium", 24), fill=MUTED)
    d.text((PAD, H - 46), _fit(d, c["sender"], font("SemiBold", 24), W - PAD * 2 - 260), font=font("SemiBold", 24),
           fill=INK)
    d.text((W - PAD - d.textlength("@Uyda_import_bot", font=font("SemiBold", 24)), H - 46), "@Uyda_import_bot",
           font=font("SemiBold", 24), fill=ORANGE)
    return _png(im)
