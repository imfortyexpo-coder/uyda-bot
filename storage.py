"""
База бота (файл SQLite, по умолчанию bot.db рядом с ботом).
Хранит работы и их статусы, историю изменений, подписчиков и отложенные напоминания.
Таблица Google при этом только читается.
"""

import sqlite3
import time

NEW, WORK, DONE, FAIL, ARCHIVE = "new", "work", "done", "fail", "archive"
OPEN_STATUSES = (NEW, WORK, FAIL)

STATUS_LABELS = {
    NEW: "🆕 Новая",
    WORK: "🔧 В работе",
    DONE: "✅ Завершён",
    FAIL: "⚠️ Не завершён",
    ARCHIVE: "🗄 Архив",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT UNIQUE NOT NULL,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    note_hash   TEXT DEFAULT '',
    opened_at   INTEGER NOT NULL,      -- когда стала открытой работой
    updated_at  INTEGER NOT NULL,
    updated_by  TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id  INTEGER NOT NULL,
    status     TEXT NOT NULL,
    reason     TEXT DEFAULT '',
    user_id    INTEGER,
    user_name  TEXT DEFAULT '',
    ts         INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS subscribers (
    user_id    INTEGER PRIMARY KEY,
    name       TEXT DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS snoozes (
    record_id  INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    remind_at  INTEGER NOT NULL,
    PRIMARY KEY (record_id, user_id)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    name        TEXT DEFAULT '',
    username    TEXT DEFAULT '',
    role        TEXT NOT NULL,         -- director / developer / operator / customs / staff / viewer / pending / denied
    requested_at INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    updated_by  TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS risks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    ref         TEXT NOT NULL DEFAULT '',     -- c:<id партии>, s:<поставщик>, k:<страна>, bot:…, manual
    title       TEXT NOT NULL DEFAULT '',
    why         TEXT NOT NULL DEFAULT '',
    p           INTEGER NOT NULL DEFAULT 3,   -- вероятность 1–5
    i           INTEGER NOT NULL DEFAULT 3,   -- влияние 1–5
    owner       TEXT NOT NULL DEFAULT '',     -- customs / operator / director / developer
    status      TEXT NOT NULL DEFAULT 'open', -- open / work / closed / accepted
    auto        INTEGER NOT NULL DEFAULT 1,
    note        TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT '',
    closed_at   INTEGER,
    notified    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cbu_rates (day TEXT PRIMARY KEY, rate REAL, fetched_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sla_notices (record_id INTEGER NOT NULL, kind TEXT NOT NULL, day TEXT NOT NULL,
                                        PRIMARY KEY (record_id, kind, day));
CREATE TABLE IF NOT EXISTS user_lang (user_id INTEGER PRIMARY KEY, lang TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS resp_state (record_id INTEGER PRIMARY KEY, resp TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS remarks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id   INTEGER NOT NULL,
    author_id   INTEGER,
    author_name TEXT DEFAULT '',
    text        TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    acked_by    TEXT DEFAULT '',
    acked_at    INTEGER
);
CREATE TABLE IF NOT EXISTS support_threads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    user_name   TEXT DEFAULT '',
    record_id   INTEGER,
    topic       TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS support_msgs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id   INTEGER NOT NULL,
    from_id     INTEGER,
    from_name   TEXT DEFAULT '',
    from_role   TEXT DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'msg',   -- msg / remark
    text        TEXT NOT NULL,
    ts          INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS docs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id      INTEGER NOT NULL,            -- партия (id первой части)
    cargo_title    TEXT DEFAULT '',
    batch          INTEGER,                     -- файлы, отправленные вместе, — одна пачка
    kind           TEXT NOT NULL DEFAULT 'document',   -- document / photo
    file_id        TEXT NOT NULL,
    file_unique_id TEXT DEFAULT '',
    name           TEXT DEFAULT '',
    size           INTEGER DEFAULT 0,
    mime           TEXT DEFAULT '',
    from_id        INTEGER,
    from_name      TEXT DEFAULT '',
    from_role      TEXT DEFAULT '',
    to_roles       TEXT DEFAULT '',             -- customs,operator,director
    note           TEXT DEFAULT '',
    path           TEXT DEFAULT '',             -- копия в папке бота
    created_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_docs_rec ON docs(record_id);
CREATE TABLE IF NOT EXISTS user_access (
    user_id  INTEGER NOT NULL,
    key      TEXT NOT NULL,                 -- docs / docs_work / dash / risks / excel / week
    allowed  INTEGER NOT NULL,              -- 1 — есть доступ, 0 — нет (иначе — по роли)
    updated_by TEXT DEFAULT '',
    updated_at INTEGER,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS doc_recv (
    doc_id     INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    user_name  TEXT DEFAULT '',
    sent_at    INTEGER,
    acked_at   INTEGER,
    PRIMARY KEY (doc_id, user_id)
);
CREATE TABLE IF NOT EXISTS role_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    target_id   INTEGER NOT NULL,
    old_role    TEXT DEFAULT '',
    new_role    TEXT NOT NULL,
    actor_id    INTEGER,
    actor_name  TEXT DEFAULT ''
);
"""

# Роли пользователей (в базе хранятся эти коды)
ADMIN = "admin"            # администратор из файла env (OWNER_IDS), пока ему не назначена должность
DIRECTOR = "director"      # Генеральный Директор
DEVELOPER = "developer"    # Разработчик
OPERATOR = "operator"      # 1С УНФ-Оператор
CUSTOMS = "customs"        # Специалист по таможенному оформлению
STAFF = "staff"            # Сотрудник
VIEWER = "viewer"          # Наблюдатель
PENDING, DENIED = "pending", "denied"   # ждёт решения / доступ закрыт
ADMIN_ROLES = (ADMIN, DIRECTOR, DEVELOPER)           # полный доступ + управление командой
EDIT_ROLES = ADMIN_ROLES + (OPERATOR, CUSTOMS, STAFF)  # могут менять статусы
ACTIVE_ROLES = EDIT_ROLES + (VIEWER,)                # доступ к данным открыт
NOTIFY_ROLES = ADMIN_ROLES + (OPERATOR,)             # получают уведомление о каждой смене статуса


class Storage:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self):
        """Новые столбцы для старых баз — добавляются сами, данные не трогаются."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(records)")}
        if "warn" not in cols:          # пометка «закрыт с предупреждением» (пустые / жёлтые ячейки)
            self.db.execute("ALTER TABLE records ADD COLUMN warn TEXT DEFAULT ''")
        if "yellow_hash" not in cols:   # какие жёлтые ячейки были при последней проверке
            self.db.execute("ALTER TABLE records ADD COLUMN yellow_hash TEXT")
        hcols = {r["name"] for r in self.db.execute("PRAGMA table_info(history)")}
        if "warn" not in hcols:
            self.db.execute("ALTER TABLE history ADD COLUMN warn TEXT DEFAULT ''")
        rcols = {r["name"] for r in self.db.execute("PRAGMA table_info(resp_state)")}
        if rcols and "since" not in rcols:   # с какого момента груз у текущего ответственного
            self.db.execute("ALTER TABLE resp_state ADD COLUMN since INTEGER")
        ucols = {r["name"] for r in self.db.execute("PRAGMA table_info(users)")}
        if "extra_roles" not in ucols:      # дополнительные роли человека (через запятую)
            self.db.execute("ALTER TABLE users ADD COLUMN extra_roles TEXT DEFAULT ''")
        dcols = {r["name"] for r in self.db.execute("PRAGMA table_info(docs)")}
        if dcols and "stage" not in dcols:   # рабочий / финальный файл
            self.db.execute("ALTER TABLE docs ADD COLUMN stage TEXT NOT NULL DEFAULT 'work'")
            self.db.execute("ALTER TABLE docs ADD COLUMN final_at INTEGER")
            self.db.execute("ALTER TABLE docs ADD COLUMN final_by TEXT DEFAULT ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_history_ts ON history(ts)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_history_rec ON history(record_id, id)")

    def commit(self):
        self.db.commit()

    # --- служебное ---------------------------------------------------------
    def get_meta(self, k: str, default: str = "") -> str:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else default

    def set_meta(self, k: str, v: str):
        self.db.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.db.commit()

    def file_size(self) -> int:
        """Размер файла базы в байтах (0 — если база в памяти)."""
        import os
        row = self.db.execute("PRAGMA database_list").fetchone()
        path = row["file"] if row else ""
        return os.path.getsize(path) if path and os.path.exists(path) else 0

    # --- работы ------------------------------------------------------------
    def get(self, record_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()

    def all_by_key(self) -> dict[str, sqlite3.Row]:
        return {r["key"]: r for r in self.db.execute("SELECT * FROM records")}

    def insert(self, key: str, title: str, status: str, note_hash: str) -> int:
        now = int(time.time())
        cur = self.db.execute(
            "INSERT INTO records(key, title, status, note_hash, opened_at, updated_at, updated_by) "
            "VALUES(?, ?, ?, ?, ?, ?, 'таблица')", (key, title, status, note_hash, now, now))
        return cur.lastrowid

    def update_title(self, record_id: int, title: str):
        self.db.execute("UPDATE records SET title=? WHERE id=?", (title, record_id))

    def set_note_hash(self, record_id: int, note_hash: str):
        self.db.execute("UPDATE records SET note_hash=? WHERE id=?", (note_hash, record_id))

    def set_status(self, record_id: int, status: str, reason: str = "",
                   user_id: int | None = None, user_name: str = "", commit: bool = True, warn: str = ""):
        """warn — пометка «закрыт с предупреждением» (что было не заполнено / не подтверждено)."""
        now = int(time.time())
        old = self.get(record_id)
        reopened = old and old["status"] not in OPEN_STATUSES and status in OPEN_STATUSES
        self.db.execute(
            "UPDATE records SET status=?, reason=?, warn=?, updated_at=?, updated_by=?"
            + (", opened_at=?" if reopened else "") + " WHERE id=?",
            (status, reason, warn, now, user_name or "таблица") + ((now,) if reopened else ()) + (record_id,))
        self.db.execute(
            "INSERT INTO history(record_id, status, reason, user_id, user_name, ts, warn) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (record_id, status, reason, user_id, user_name, now, warn))
        if status not in OPEN_STATUSES:
            self.db.execute("DELETE FROM snoozes WHERE record_id=?", (record_id,))
        if commit:
            self.db.commit()

    def open_records(self) -> list[sqlite3.Row]:
        q = f"SELECT * FROM records WHERE status IN ({','.join('?' * len(OPEN_STATUSES))}) ORDER BY opened_at"
        return list(self.db.execute(q, OPEN_STATUSES))

    def done_since(self, ts: int) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM records WHERE status=? AND updated_at>=? ORDER BY updated_at", (DONE, ts)))

    def set_yellow_hash(self, record_id: int, value: str):
        self.db.execute("UPDATE records SET yellow_hash=? WHERE id=?", (value, record_id))

    def warned_since(self, ts: int) -> list[sqlite3.Row]:
        """Закрытые с предупреждением после ts (и до сих пор закрытые)."""
        return list(self.db.execute(
            "SELECT * FROM records WHERE status=? AND warn<>'' AND updated_at>=? ORDER BY updated_at DESC", (DONE, ts)))

    # --- журнал действий ---------------------------------------------------
    def journal(self, limit: int = 15, offset: int = 0, people_only: bool = True) -> list[dict]:
        """Последние смены статусов: кто, что, когда, было → стало. Новые сверху."""
        where = "WHERE h.user_id IS NOT NULL" if people_only else ""
        rows = list(self.db.execute(
            f"SELECT h.*, r.title AS title, "
            f"(SELECT p.status FROM history p WHERE p.record_id=h.record_id AND p.id<h.id ORDER BY p.id DESC LIMIT 1) AS old "
            f"FROM history h JOIN records r ON r.id=h.record_id {where} ORDER BY h.id DESC LIMIT ? OFFSET ?",
            (limit, offset)))
        return [dict(r) for r in rows]

    def journal_count(self, people_only: bool = True) -> int:
        where = "WHERE user_id IS NOT NULL" if people_only else ""
        return self.db.execute(f"SELECT COUNT(*) FROM history {where}").fetchone()[0]

    def people_stats(self, since: int) -> list[dict]:
        """По сотрудникам с даты since: сколько взяли в работу, закрыли, закрыли с пометкой, указали причину."""
        rows = self.db.execute(
            "SELECT user_id, MAX(user_name) AS name, "
            "SUM(status='work') AS taken, SUM(status='done') AS closed, "
            "SUM(status='done' AND warn<>'') AS warned, SUM(status='fail') AS failed, COUNT(*) AS actions "
            "FROM history WHERE user_id IS NOT NULL AND ts>=? GROUP BY user_id ORDER BY closed DESC, actions DESC",
            (since,))
        return [dict(r) for r in rows]

    def closed_count_since(self, ts: int) -> int:
        return self.db.execute(
            "SELECT COUNT(DISTINCT record_id) FROM history WHERE status=? AND user_id IS NOT NULL AND ts>=?",
            (DONE, ts)).fetchone()[0]

    def last_change(self, record_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM history WHERE record_id=? ORDER BY id DESC LIMIT 1", (record_id,)).fetchone()

    # --- подписчики --------------------------------------------------------
    def add_subscriber(self, user_id: int, name: str):
        self.db.execute(
            "INSERT INTO subscribers(user_id, name, created_at) VALUES(?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name", (user_id, name, int(time.time())))
        self.db.commit()

    def remove_subscriber(self, user_id: int):
        self.db.execute("DELETE FROM subscribers WHERE user_id=?", (user_id,))
        self.db.commit()

    def subscribers(self) -> list[int]:
        return [r["user_id"] for r in self.db.execute("SELECT user_id FROM subscribers")]

    def subscriber_name(self, user_id: int) -> str:
        row = self.db.execute("SELECT name FROM subscribers WHERE user_id=?", (user_id,)).fetchone()
        return row["name"] if row else ""

    # --- пользователи и роли -----------------------------------------------
    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def list_users(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM users ORDER BY requested_at"))

    def count_users(self, role: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM users WHERE role=?", (role,)).fetchone()[0]

    def db_ids_by_roles(self, roles: tuple[str, ...]) -> set[int]:
        """ID всех, кому в боте выбрана одна из этих ролей — основная или дополнительная
        (дополнительная действует, только пока у человека открыт доступ)."""
        want, out = set(roles), set()
        for r in self.db.execute("SELECT user_id, role, COALESCE(extra_roles, '') AS extra FROM users"):
            extra = {x for x in r["extra"].split(",") if x}
            if r["role"] in want or (r["role"] in ACTIVE_ROLES and extra & want):
                out.add(r["user_id"])
        return out

    def extra_roles(self, user_id: int) -> list[str]:
        row = self.db.execute("SELECT COALESCE(extra_roles, '') FROM users WHERE user_id=?", (user_id,)).fetchone()
        return [x for x in (row[0] if row else "").split(",") if x]

    def set_extra_roles(self, user_id: int, roles: list[str], actor_id: int | None, actor_name: str,
                        change: str = "", default_role: str = ADMIN, name: str = "") -> None:
        """Записать дополнительные роли. change — '+operator' / '-operator' для журнала ролей."""
        now = int(time.time())
        if self.get_user(user_id) is None:
            self.db.execute("INSERT INTO users(user_id, name, username, role, requested_at, updated_at, updated_by) "
                            "VALUES(?, ?, '', ?, ?, ?, ?)", (user_id, name, default_role, now, now, actor_name))
        self.db.execute("UPDATE users SET extra_roles=?, updated_at=?, updated_by=? WHERE user_id=?",
                        (",".join(roles), now, actor_name, user_id))
        if change:
            cur = self.get_user(user_id)["role"]
            self.db.execute("INSERT INTO role_log(ts, target_id, old_role, new_role, actor_id, actor_name) "
                            "VALUES(?, ?, ?, ?, ?, ?)", (now, user_id, cur, change, actor_id, actor_name))
        self.db.commit()

    def db_admin_ids(self) -> set[int]:
        return self.db_ids_by_roles(ADMIN_ROLES)

    def add_user_request(self, user_id: int, name: str, username: str) -> None:
        """Новый запрос доступа (роль «ожидает»). Уже известного пользователя не трогает."""
        now = int(time.time())
        self.db.execute(
            "INSERT OR IGNORE INTO users(user_id, name, username, role, requested_at, updated_at, updated_by) "
            "VALUES(?, ?, ?, ?, ?, ?, 'запрос')", (user_id, name, username, PENDING, now, now))
        self.db.commit()

    def set_user_role(self, user_id: int, role: str, actor_id: int | None, actor_name: str,
                      name: str = "", username: str = "") -> str:
        """Назначает роль, пишет в журнал ролей. Возвращает прежнюю роль ('' — записи не было)."""
        now = int(time.time())
        old = self.get_user(user_id)
        old_role = old["role"] if old else ""
        if old:
            self.db.execute("UPDATE users SET role=?, updated_at=?, updated_by=? WHERE user_id=?",
                            (role, now, actor_name, user_id))
        else:
            self.db.execute(
                "INSERT INTO users(user_id, name, username, role, requested_at, updated_at, updated_by) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)", (user_id, name, username, role, now, now, actor_name))
        self.db.execute(
            "INSERT INTO role_log(ts, target_id, old_role, new_role, actor_id, actor_name) VALUES(?, ?, ?, ?, ?, ?)",
            (now, user_id, old_role, role, actor_id, actor_name))
        self.db.commit()
        return old_role

    def role_history(self, user_id: int, limit: int = 5) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM role_log WHERE target_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)))

    # --- язык пользователя ------------------------------------------------------
    def get_lang(self, user_id: int) -> str | None:
        row = self.db.execute("SELECT lang FROM user_lang WHERE user_id=?", (user_id,)).fetchone()
        return row["lang"] if row else None

    def set_lang(self, user_id: int, lang: str) -> None:
        self.db.execute("INSERT INTO user_lang(user_id, lang) VALUES(?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang", (user_id, lang))
        self.db.commit()

    # --- ответственный за груз ----------------------------------------------
    def resp_all(self) -> dict[int, str]:
        return {r["record_id"]: r["resp"] for r in self.db.execute("SELECT record_id, resp FROM resp_state")}

    def set_resp(self, record_id: int, resp: str, commit: bool = False) -> None:
        """Новый ответственный — отсчёт срока этапа начинается заново."""
        now = int(time.time())
        self.db.execute("INSERT INTO resp_state(record_id, resp, since) VALUES(?, ?, ?) "
                        "ON CONFLICT(record_id) DO UPDATE SET resp=excluded.resp, since=excluded.since",
                        (record_id, resp, now))
        if commit:
            self.db.commit()

    def resp_since(self) -> dict[int, int]:
        """id груза -> с какого времени он у текущего ответственного (старые записи — с первого запуска)."""
        now = int(time.time())
        self.db.execute("UPDATE resp_state SET since=? WHERE since IS NULL", (now,))
        return {r["record_id"]: r["since"] for r in self.db.execute("SELECT record_id, since FROM resp_state")}

    # --- срок этапа: напоминание отправлено сегодня? --------------------------
    def sla_mark(self, record_id: int, kind: str, day: str) -> bool:
        """True — отметка новая (сегодня ещё не напоминали)."""
        cur = self.db.execute("INSERT OR IGNORE INTO sla_notices(record_id, kind, day) VALUES(?, ?, ?)",
                              (record_id, kind, day))
        self.db.commit()
        return cur.rowcount == 1

    # --- реестр рисков ------------------------------------------------------------
    def risk_sync(self, signals: list) -> tuple[list[int], int]:
        """Авто-риски по свежим признакам: новые — добавить, изменившиеся — обновить, пропавшие — закрыть.
        Принятые (accepted) и снятые вручную не открываются снова, пока не вырос балл.
        Возвращает (id новых или выросших до оповещения, сколько закрыто)."""
        now = int(time.time())
        rows = {(r["kind"], r["ref"]): r for r in self.db.execute("SELECT * FROM risks WHERE auto=1")}
        seen, raised = set(), []
        for sg in signals:
            key = (sg.kind, sg.ref)
            seen.add(key)
            row = rows.get(key)
            if row is None:
                cur = self.db.execute(
                    "INSERT INTO risks(kind, ref, title, why, p, i, owner, status, auto, created_at, updated_at, updated_by)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?, 'бот')",
                    (sg.kind, sg.ref, sg.title, sg.why, sg.p, sg.i, sg.owner, now, now))
                raised.append(cur.lastrowid)
                continue
            old_score = row["p"] * row["i"]
            reopen = row["status"] == "closed" and row["updated_by"] == "бот"     # снят ботом — условие вернулось
            grew = row["status"] in ("accepted", "closed") and sg.score > old_score
            status = "open" if (reopen or grew) else row["status"]
            self.db.execute("UPDATE risks SET title=?, why=?, p=?, i=?, owner=?, status=?, updated_at=?,"
                            " closed_at=CASE WHEN ?='open' THEN NULL ELSE closed_at END,"
                            " notified=CASE WHEN ?>p*i THEN 0 ELSE notified END WHERE id=?",
                            (sg.title, sg.why, sg.p, sg.i, sg.owner, status, now, status, sg.score, row["id"]))
            if status == "open" and (reopen or grew or sg.score > old_score):
                raised.append(row["id"])
        closed = 0
        for key, row in rows.items():
            if key not in seen and row["status"] in ("open", "work"):
                self.db.execute("UPDATE risks SET status='closed', closed_at=?, updated_at=?, updated_by='бот',"
                                " note=CASE WHEN note='' THEN 'условие риска ушло' ELSE note END WHERE id=?",
                                (now, now, row["id"]))
                closed += 1
        self.db.commit()
        return raised, closed

    def risk_add(self, title: str, p: int, i: int, owner: str, author: str, ref: str = "manual") -> int:
        now = int(time.time())
        cur = self.db.execute(
            "INSERT INTO risks(kind, ref, title, why, p, i, owner, status, auto, created_at, updated_at, updated_by)"
            " VALUES('manual', ?, ?, '', ?, ?, ?, 'open', 0, ?, ?, ?)", (ref, title, p, i, owner, now, now, author))
        self.db.commit()
        return cur.lastrowid

    def risk_get(self, rid: int):
        return self.db.execute("SELECT * FROM risks WHERE id=?", (rid,)).fetchone()

    def risks(self, statuses=("open", "work")) -> list:
        q = f"SELECT * FROM risks WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY p*i DESC, updated_at DESC"
        return list(self.db.execute(q, statuses))

    def risks_since(self, ts: int) -> tuple[list, list]:
        new = list(self.db.execute("SELECT * FROM risks WHERE created_at>=? ORDER BY p*i DESC", (ts,)))
        closed = list(self.db.execute("SELECT * FROM risks WHERE closed_at>=? ORDER BY p*i DESC", (ts,)))
        return new, closed

    def risk_update(self, rid: int, who: str, **fields) -> None:
        now = int(time.time())
        fields["updated_at"], fields["updated_by"] = now, who
        if fields.get("status") in ("closed", "accepted"):
            fields["closed_at"] = now
        elif fields.get("status") in ("open", "work"):
            fields["closed_at"] = None
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE risks SET {cols} WHERE id=?", (*fields.values(), rid))
        self.db.commit()

    def risk_mark_notified(self, rid: int) -> None:
        self.db.execute("UPDATE risks SET notified=1 WHERE id=?", (rid,))
        self.db.commit()

    # --- курс ЦБ ------------------------------------------------------------------
    def cbu_get(self, day: str):
        return self.db.execute("SELECT rate, fetched_at FROM cbu_rates WHERE day=?", (day,)).fetchone()

    def cbu_put(self, day: str, rate: float | None) -> None:
        self.db.execute("INSERT INTO cbu_rates(day, rate, fetched_at) VALUES(?, ?, ?) "
                        "ON CONFLICT(day) DO UPDATE SET rate=excluded.rate, fetched_at=excluded.fetched_at",
                        (day, rate, int(time.time())))
        self.db.commit()

    def cbu_all(self) -> dict[str, float]:
        return {r["day"]: r["rate"] for r in self.db.execute("SELECT day, rate FROM cbu_rates WHERE rate IS NOT NULL")}

    # --- замечания Генерального Директора по грузам ----------------------------
    def add_remark(self, record_id: int, author_id: int, author_name: str, text: str) -> int:
        cur = self.db.execute("INSERT INTO remarks(record_id, author_id, author_name, text, created_at) "
                              "VALUES(?, ?, ?, ?, ?)", (record_id, author_id, author_name, text, int(time.time())))
        self.db.commit()
        return cur.lastrowid

    def open_remarks(self, record_id: int | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM remarks WHERE acked_at IS NULL", ()
        if record_id is not None:
            q, args = q + " AND record_id=?", (record_id,)
        return list(self.db.execute(q + " ORDER BY id", args))

    def ack_remarks(self, record_id: int, user_name: str) -> int:
        cur = self.db.execute("UPDATE remarks SET acked_by=?, acked_at=? WHERE record_id=? AND acked_at IS NULL",
                              (user_name, int(time.time()), record_id))
        self.db.commit()
        return cur.rowcount

    # --- окно поддержки ---------------------------------------------------------
    def thread_open_of(self, user_id: int):
        return self.db.execute("SELECT * FROM support_threads WHERE user_id=? AND status='open' "
                               "ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()

    def thread_new(self, user_id: int, user_name: str, record_id: int | None, topic: str) -> int:
        now = int(time.time())
        cur = self.db.execute("INSERT INTO support_threads(user_id, user_name, record_id, topic, created_at, updated_at) "
                              "VALUES(?, ?, ?, ?, ?, ?)", (user_id, user_name, record_id, topic[:80], now, now))
        self.db.commit()
        return cur.lastrowid

    def thread_get(self, tid: int):
        return self.db.execute("SELECT * FROM support_threads WHERE id=?", (tid,)).fetchone()

    def threads_open(self, limit: int = 15) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM support_threads WHERE status='open' "
                                    "ORDER BY updated_at DESC LIMIT ?", (limit,)))

    def thread_close(self, tid: int) -> None:
        self.db.execute("UPDATE support_threads SET status='closed', updated_at=? WHERE id=?", (int(time.time()), tid))
        self.db.commit()

    def msg_add(self, tid: int, from_id: int, from_name: str, from_role: str, text: str, kind: str = "msg") -> None:
        now = int(time.time())
        self.db.execute("INSERT INTO support_msgs(thread_id, from_id, from_name, from_role, kind, text, ts) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?)", (tid, from_id, from_name, from_role, kind, text, now))
        self.db.execute("UPDATE support_threads SET updated_at=? WHERE id=?", (now, tid))
        self.db.commit()

    def msgs(self, tid: int, limit: int = 8) -> list[sqlite3.Row]:
        rows = list(self.db.execute("SELECT * FROM support_msgs WHERE thread_id=? ORDER BY id DESC LIMIT ?",
                                    (tid, limit)))
        return rows[::-1]

    # --- документы партий ----------------------------------------------------------
    def doc_add(self, record_id: int, cargo_title: str, batch: int | None, f: dict, from_id: int, from_name: str,
                from_role: str, to_roles: str, note: str, stage: str = "work") -> int:
        now = int(time.time())
        cur = self.db.execute(
            "INSERT INTO docs(record_id, cargo_title, batch, kind, file_id, file_unique_id, name, size, mime, from_id, "
            "from_name, from_role, to_roles, note, created_at, stage, final_at, final_by) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record_id, cargo_title, batch, f["kind"], f["file_id"], f.get("file_unique_id", ""), f["name"],
             int(f.get("size") or 0), f.get("mime", ""), from_id, from_name, from_role, to_roles, note, now,
             stage, now if stage == "final" else None, from_name if stage == "final" else ""))
        did = cur.lastrowid
        if batch is None:
            self.db.execute("UPDATE docs SET batch=? WHERE id=?", (did, did))
        self.db.commit()
        return did

    def doc_get(self, doc_id: int):
        return self.db.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()

    def doc_make_final(self, doc_id: int, by: str, to_roles: str) -> None:
        self.db.execute("UPDATE docs SET stage='final', final_at=?, final_by=?, to_roles=? WHERE id=?",
                        (int(time.time()), by, to_roles, doc_id))
        self.db.commit()

    # --- доступы к разделам (галочки Директора) -------------------------------------
    def access_of(self, user_id: int) -> dict[str, bool]:
        return {r["key"]: bool(r["allowed"]) for r in
                self.db.execute("SELECT key, allowed FROM user_access WHERE user_id=?", (user_id,))}

    def access_set(self, user_id: int, key: str, allowed: bool | None, by: str = "") -> None:
        if allowed is None:
            self.db.execute("DELETE FROM user_access WHERE user_id=? AND key=?", (user_id, key))
        else:
            self.db.execute("INSERT INTO user_access(user_id, key, allowed, updated_by, updated_at) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(user_id, key) DO UPDATE SET allowed=excluded.allowed, "
                            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                            (user_id, key, int(allowed), by, int(time.time())))
        self.db.commit()

    def access_reset(self, user_id: int) -> None:
        self.db.execute("DELETE FROM user_access WHERE user_id=?", (user_id,))
        self.db.commit()

    def doc_set_path(self, doc_id: int, path: str) -> None:
        self.db.execute("UPDATE docs SET path=? WHERE id=?", (path, doc_id))
        self.db.commit()

    def docs_for(self, record_ids: list[int]) -> list[sqlite3.Row]:
        if not record_ids:
            return []
        q = ",".join("?" * len(record_ids))
        return list(self.db.execute(f"SELECT * FROM docs WHERE record_id IN ({q}) ORDER BY id DESC", record_ids))

    def docs_all(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM docs ORDER BY id DESC"))

    def doc_recv_add(self, doc_id: int, user_id: int, user_name: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO doc_recv(doc_id, user_id, user_name, sent_at) VALUES(?, ?, ?, ?)",
                        (doc_id, user_id, user_name, int(time.time())))
        self.db.commit()

    def doc_recv(self, doc_id: int) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM doc_recv WHERE doc_id=? ORDER BY sent_at", (doc_id,)))

    def doc_recv_map(self, doc_ids: list[int]) -> dict[int, list[sqlite3.Row]]:
        out: dict[int, list] = {}
        if not doc_ids:
            return out
        q = ",".join("?" * len(doc_ids))
        for r in self.db.execute(f"SELECT * FROM doc_recv WHERE doc_id IN ({q}) ORDER BY sent_at", doc_ids):
            out.setdefault(r["doc_id"], []).append(r)
        return out

    def doc_ack_batch(self, doc_id: int, user_id: int) -> list[sqlite3.Row]:
        """«Принято» по файлу — отмечаются все неподтверждённые файлы той же пачки. Возвращает отмеченные."""
        d = self.doc_get(doc_id)
        if d is None:
            return []
        rows = list(self.db.execute(
            "SELECT d.* FROM docs d JOIN doc_recv r ON r.doc_id=d.id WHERE d.batch=? AND r.user_id=? "
            "AND r.acked_at IS NULL ORDER BY d.id", (d["batch"], user_id)))
        if rows:
            ids = [r["id"] for r in rows]
            q = ",".join("?" * len(ids))
            self.db.execute(f"UPDATE doc_recv SET acked_at=? WHERE user_id=? AND doc_id IN ({q})",
                            [int(time.time()), user_id] + ids)
            self.db.commit()
        return rows

    def docs_unacked(self, user_id: int) -> list[sqlite3.Row]:
        """Файлы, которые прислали этому человеку и он ещё не нажал «Принято»."""
        return list(self.db.execute(
            "SELECT d.* FROM docs d JOIN doc_recv r ON r.doc_id=d.id WHERE r.user_id=? AND r.acked_at IS NULL "
            "ORDER BY d.id DESC", (user_id,)))

    def doc_counts(self, record_ids: list[int]) -> tuple[int, int]:
        """(всего файлов у партии, из них кто-то ещё не принял)."""
        if not record_ids:
            return 0, 0
        q = ",".join("?" * len(record_ids))
        total = self.db.execute(f"SELECT COUNT(*) FROM docs WHERE record_id IN ({q})", record_ids).fetchone()[0]
        waiting = self.db.execute(
            f"SELECT COUNT(DISTINCT d.id) FROM docs d JOIN doc_recv r ON r.doc_id=d.id "
            f"WHERE d.record_id IN ({q}) AND r.acked_at IS NULL", record_ids).fetchone()[0]
        return total, waiting

    def docs_recent_records(self, limit: int = 8, final_only: bool = False) -> list[int]:
        where = "WHERE stage='final' " if final_only else ""
        return [r[0] for r in self.db.execute(
            f"SELECT record_id, MAX(id) m FROM docs {where}GROUP BY record_id ORDER BY m DESC LIMIT ?", (limit,))]

    # --- «Напомнить позже» -------------------------------------------------
    def set_snooze(self, record_id: int, user_id: int, remind_at: int):
        self.db.execute(
            "INSERT INTO snoozes(record_id, user_id, remind_at) VALUES(?, ?, ?) "
            "ON CONFLICT(record_id, user_id) DO UPDATE SET remind_at=excluded.remind_at",
            (record_id, user_id, remind_at))
        self.db.commit()

    def pop_due_snoozes(self, now: int) -> list[sqlite3.Row]:
        rows = list(self.db.execute("SELECT * FROM snoozes WHERE remind_at<=?", (now,)))
        self.db.execute("DELETE FROM snoozes WHERE remind_at<=?", (now,))
        self.db.commit()
        return rows
