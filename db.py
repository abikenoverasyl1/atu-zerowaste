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
    "cafe": "Кофейня",
    "inn": "Гостиница и общепит",
    "dept_lab": "Кафедра (учебная лаборатория)",
    "buffet": "Буфет",
    "sci_lab": "Научная лаборатория",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,          -- используется в QR-ссылке /f/<slug>
    type TEXT NOT NULL,                 -- см. ORG_TYPES
    building TEXT,
    work_days TEXT NOT NULL DEFAULT '12345', -- дни, когда обычно есть взвешивания (1=пн), '' = по расписанию занятий
    pin TEXT,                           -- 4 цифры: вход по QR без логина и пароля
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    login TEXT UNIQUE NOT NULL,
    email TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,                 -- см. ROLES
    org_id INTEGER REFERENCES organizations(id),
    telegram_chat_id TEXT
);

-- Справочник способов переработки / утилизации (как в таблицах учёта АТУ)
CREATE TABLE IF NOT EXISTS disposal_methods (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    recycled INTEGER NOT NULL DEFAULT 0     -- 1 = отходы реально идут в переработку, а не на полигон
);

CREATE TABLE IF NOT EXISTS waste_types (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'кг',    -- кг или л
    recyclable INTEGER NOT NULL DEFAULT 1,   -- пригоден к переработке
    edible INTEGER NOT NULL DEFAULT 0,       -- может быть съедобным излишком
    food INTEGER NOT NULL DEFAULT 1,         -- пищевой / органический (для CO2 при захоронении)
    price_per_unit REAL NOT NULL DEFAULT 0,  -- стоимость сырья, ₸ за кг/л (оценка)
    co2_factor REAL NOT NULL DEFAULT 0.627,  -- кг CO2e на кг при захоронении (DEFRA, пищевые)
    default_method_id INTEGER REFERENCES disposal_methods(id),
    active INTEGER NOT NULL DEFAULT 1        -- виды с записями не удаляются, только деактивируются
);

-- Шаблон формы: какие виды отходов есть у организации (по образцу её таблицы)
CREATE TABLE IF NOT EXISTS org_waste_types (
    org_id INTEGER REFERENCES organizations(id),
    waste_type_id INTEGER REFERENCES waste_types(id),
    sort INTEGER DEFAULT 0,
    PRIMARY KEY (org_id, waste_type_id)
);

-- Отчёт = все записи организации за месяц. date = первый день месяца (YYYY-MM-01)
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY,
    org_id INTEGER NOT NULL REFERENCES organizations(id),
    date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    note TEXT,
    created_by INTEGER REFERENCES users(id),
    updated_at TEXT,
    UNIQUE (org_id, date)
);

-- Запись = одно взвешивание (строка журнала: дата, наименование, вес/объём, способ, ответственный)
CREATE TABLE IF NOT EXISTS report_items (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    waste_type_id INTEGER NOT NULL REFERENCES waste_types(id),
    qty REAL NOT NULL DEFAULT 0,
    surplus_qty REAL NOT NULL DEFAULT 0,  -- из них съедобный излишок, отданный, а не выброшенный
    method_id INTEGER REFERENCES disposal_methods(id),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT,
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
CREATE INDEX IF NOT EXISTS idx_items_date ON report_items(date);
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
