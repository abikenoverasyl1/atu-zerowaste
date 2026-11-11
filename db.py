"""База данных: SQLite, без ORM — чтобы любой в команде мог прочитать SQL."""
import os
import sqlite3

from flask import g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "zerowaste.db"))

# Справочник ролей
ROLES = {
    "responsible": "Ответственное лицо",
    "reviewer": "Проверяющий",
    "admin": "Администратор",
    "management": "Руководство",
}

# Статусы отчёта (жизненный цикл формы)
STATUSES = {
    "draft": "Черновик",
    "submitted": "Отправлено",
    "in_review": "На проверке",
    "accepted": "Принято",
    "returned": "Возвращено",
}

ORG_TYPES = {
    "canteen": "Столовая",
    "buffet": "Буфет",
    "inn": "Ресторанно-гостиничный комплекс",
    "edu_lab": "Учебная лаборатория",
    "sci_lab": "Научная лаборатория",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,          -- используется в QR-ссылке /f/<slug>
    type TEXT NOT NULL,                 -- см. ORG_TYPES
    building TEXT,
    work_days TEXT NOT NULL DEFAULT '12345', -- дни недели, когда точка сдаёт отчёт (1=пн)
    pin TEXT                            -- 4 цифры: вход по QR без логина и пароля
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    login TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,                 -- см. ROLES
    org_id INTEGER REFERENCES organizations(id),
    telegram_chat_id TEXT
);

CREATE TABLE IF NOT EXISTS waste_types (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'кг',    -- кг или л
    recyclable INTEGER NOT NULL DEFAULT 1,   -- пригоден к переработке (корм/компост)
    edible INTEGER NOT NULL DEFAULT 0,       -- может быть съедобным излишком
    price_per_unit REAL NOT NULL DEFAULT 0,  -- стоимость сырья, ₸ за кг/л (оценка)
    co2_factor REAL NOT NULL DEFAULT 0.627   -- кг CO2e на кг при захоронении (DEFRA)
);

-- Шаблон формы: какие виды отходов есть у организации
CREATE TABLE IF NOT EXISTS org_waste_types (
    org_id INTEGER REFERENCES organizations(id),
    waste_type_id INTEGER REFERENCES waste_types(id),
    sort INTEGER DEFAULT 0,
    PRIMARY KEY (org_id, waste_type_id)
);

-- Отчёт = форма одной организации за один день
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    org_id INTEGER NOT NULL REFERENCES organizations(id),
    date TEXT NOT NULL,                 -- YYYY-MM-DD
    status TEXT NOT NULL DEFAULT 'draft',
    note TEXT,
    created_by INTEGER REFERENCES users(id),
    updated_at TEXT,
    UNIQUE (org_id, date)
);

CREATE TABLE IF NOT EXISTS report_items (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    waste_type_id INTEGER NOT NULL REFERENCES waste_types(id),
    qty REAL NOT NULL DEFAULT 0,
    surplus_qty REAL NOT NULL DEFAULT 0,  -- из них съедобный излишок, отданный, а не выброшенный
    photo TEXT
);

-- История изменений статуса + комментарии проверяющего
CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id),
    comment TEXT,
    ts TEXT NOT NULL
);

-- Объявления о съедобных излишках («не выбросить, а отдать»)
CREATE TABLE IF NOT EXISTS surplus_posts (
    id INTEGER PRIMARY KEY,
    org_id INTEGER NOT NULL REFERENCES organizations(id),
    text TEXT NOT NULL,
    until TEXT,
    ts TEXT NOT NULL,
    sent_to_telegram INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(date);
CREATE INDEX IF NOT EXISTS idx_items_report ON report_items(report_id);
"""


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()
