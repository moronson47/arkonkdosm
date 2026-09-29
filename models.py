"""Схема БД SQLite и вспомогательные функции доступа к данным."""
import os
import sqlite3

from flask import current_app, g

USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    login                 TEXT    NOT NULL UNIQUE,
    password_hash         TEXT    NOT NULL,
    full_name             TEXT    NOT NULL DEFAULT '',
    role                  TEXT    NOT NULL CHECK (role IN ('admin', 'expert', 'observer')),
    is_active             INTEGER NOT NULL DEFAULT 1,
    must_change_password  INTEGER NOT NULL DEFAULT 0,
    last_login            TEXT,
    created_at            TEXT    NOT NULL
);
"""

SCHEMA = USERS_TABLE + """
CREATE TABLE IF NOT EXISTS teams (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    order_num  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    order_num  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS blocks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    stage_id   INTEGER NOT NULL REFERENCES stages(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    order_num  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS criteria (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    block_id     INTEGER NOT NULL REFERENCES blocks(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    max_score    INTEGER NOT NULL,
    order_num    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    expert_id     INTEGER NOT NULL REFERENCES users(id)     ON DELETE CASCADE,
    team_id       INTEGER NOT NULL REFERENCES teams(id)     ON DELETE CASCADE,
    criterion_id  INTEGER NOT NULL REFERENCES criteria(id)  ON DELETE CASCADE,
    value         INTEGER,
    comment       TEXT,
    updated_at    TEXT,
    UNIQUE (expert_id, team_id, criterion_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action      TEXT    NOT NULL,
    details     TEXT,
    ip          TEXT,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS login_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ip            TEXT NOT NULL,
    attempted_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scores_lookup    ON scores(expert_id, team_id);
CREATE INDEX IF NOT EXISTS idx_scores_team      ON scores(team_id, criterion_id);
CREATE INDEX IF NOT EXISTS idx_blocks_stage     ON blocks(stage_id);
CREATE INDEX IF NOT EXISTS idx_criteria_block   ON criteria(block_id);
CREATE INDEX IF NOT EXISTS idx_attempts_ip      ON login_attempts(ip, attempted_at);
CREATE INDEX IF NOT EXISTS idx_audit_created    ON audit_log(created_at);
"""


def get_db():
    """Возвращает соединение SQLite, привязанное к контексту запроса."""
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE_PATH"], timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 5000")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _migrate_role_constraint(db):
    """Добавляет роль 'observer' в CHECK-ограничение users для старых БД.

    SQLite не позволяет изменить CHECK на месте, поэтому таблица users
    пересоздаётся с сохранением данных. Foreign keys временно отключаются,
    а legacy_alter_table предотвращает переписывание ссылок в других таблицах.
    """
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'users'"
    ).fetchone()
    if row is None:
        return
    if "'observer'" in (row["sql"] or ""):
        return

    old_isolation = db.isolation_level
    db.isolation_level = None
    try:
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("PRAGMA legacy_alter_table = ON")
        db.execute("ALTER TABLE users RENAME TO users_old")
        db.executescript(USERS_TABLE)
        db.execute(
            "INSERT INTO users(id, login, password_hash, full_name, role, is_active, "
            "must_change_password, last_login, created_at) "
            "SELECT id, login, password_hash, full_name, role, is_active, "
            "must_change_password, last_login, created_at FROM users_old"
        )
        db.execute("DROP TABLE users_old")
        db.execute("PRAGMA legacy_alter_table = OFF")
        db.execute("PRAGMA foreign_keys = ON")
    finally:
        db.isolation_level = old_isolation


def init_db(force=False):
    """Создаёт схему и наполняет БД. force=True — пересоздать с нуля."""
    path = current_app.config["DATABASE_PATH"]
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    if force and os.path.exists(path):
        for suffix in ("", "-wal", "-shm"):
            candidate = path + suffix
            if os.path.exists(candidate):
                os.remove(candidate)

    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    db.commit()

    _migrate_role_constraint(db)

    from seed_data import seed

    seed(db)

    db.commit()
    db.close()


def init_app(app):
    app.teardown_appcontext(close_db)
