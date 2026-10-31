"""
Надёжность бота: автокопии базы, слежение за чтением таблицы, проверка свежести Excel-файла,
необязательный «пульс» во внешний сервис. Здесь только логика без Telegram — её легко проверить отдельно.
"""

import logging
import os
import sqlite3
import time
import urllib.request
from datetime import datetime

log = logging.getLogger("import-bot")

AUTO_PREFIX = "auto_"


# ---------------------------------------------------------------------------
# Автокопии bot.db
# ---------------------------------------------------------------------------
def make_backup(db: sqlite3.Connection, folder: str, keep: int, now: datetime | None = None) -> str:
    """Делает копию базы (безопасно, пока бот работает), оставляет последние `keep` автокопий.
    Возвращает путь к новому файлу. Копии, сделанные вручную (backup_db.bat), не трогает."""
    os.makedirs(folder, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(folder, f"{AUTO_PREFIX}{stamp}.db")
    dst = sqlite3.connect(path)
    try:
        db.backup(dst)
    finally:
        dst.close()
    prune_backups(folder, keep)
    return path


def list_backups(folder: str) -> list[str]:
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.startswith(AUTO_PREFIX) and f.endswith(".db"))


def prune_backups(folder: str, keep: int) -> None:
    files = list_backups(folder)
    for name in files[:max(0, len(files) - max(1, keep))]:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            log.warning("Не удалось удалить старую копию %s", name)


# ---------------------------------------------------------------------------
# Слежение за чтением таблицы
# ---------------------------------------------------------------------------
class SheetWatch:
    """Помнит, читается ли таблица. ok()/fail() вызываются после каждой попытки чтения.
    fail() возвращает ('alert', секунд) один раз, когда таблица не читается дольше порога;
    ok() возвращает ('recovered', секунд), если до этого тревога уже была отправлена."""

    def __init__(self, alert_after_seconds: float):
        self.alert_after = alert_after_seconds
        self.down_since: float | None = None
        self.alerted = False
        self.last_ok: float | None = None
        self.last_error = ""

    @property
    def is_down(self) -> bool:
        return self.down_since is not None

    def ok(self, now: float) -> tuple[str, float] | None:
        action = ("recovered", now - self.down_since) if self.alerted and self.down_since else None
        self.last_ok = now
        self.down_since = None
        self.alerted = False
        self.last_error = ""
        return action

    def fail(self, now: float, error: str) -> tuple[str, float] | None:
        self.last_error = error
        if self.down_since is None:
            self.down_since = now
        if not self.alerted and now - self.down_since >= self.alert_after:
            self.alerted = True
            return ("alert", now - self.down_since)
        return None


# ---------------------------------------------------------------------------
# Свежесть Excel-файла
# ---------------------------------------------------------------------------
def file_age(path: str, now: float | None = None) -> tuple[float, float] | None:
    """(время изменения файла, возраст в секундах) или None, если файла нет."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return mtime, (now or time.time()) - mtime


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "меньше минуты"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} мин."
    hours = minutes // 60
    if hours < 48:
        return f"{hours} ч."
    return f"{hours // 24} дн."


def human_size(n: int) -> str:
    return f"{n / 1024:.0f} КБ" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} МБ"


# ---------------------------------------------------------------------------
# «Пульс» — необязательный адрес внешнего сервиса (healthchecks.io, UptimeRobot и т. п.)
# ---------------------------------------------------------------------------
def ping(url: str, timeout: float = 10) -> bool:
    """Обычный GET-запрос на адрес. Любая ошибка — просто False, боту она не мешает."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False
