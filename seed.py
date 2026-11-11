"""Генератор демо-данных: 9 организаций АТУ, ~3 месяца ежедневных отчётов.

Все объёмы — ОЦЕНКИ для демонстрации (не реальные данные АТУ):
опора — исследования университетских столовых (~60 г отходов на порцию),
коэффициент CO2 — DEFRA 2022 (0,627 кг CO2e на кг пищевых отходов на полигоне).

Запуск:  python seed.py          (пересоздаёт zerowaste.db)
"""
import os
import random
from datetime import date, datetime, timedelta

from werkzeug.security import generate_password_hash

import db

random.seed(42)
TODAY = date.today()
START = TODAY - timedelta(days=95)   # чуть больше 3 месяцев истории по каждой точке
DEMO_PASSWORD = "demo"

# ---------- Виды отходов ----------
# (ключ, название, ед., пригоден к переработке, может быть съедобным излишком, ₸ за ед. сырья — оценка)
WASTE_TYPES = [
    ("plate", "Остатки с тарелок", "кг", 1, 0, 450),
    ("bread", "Хлеб и выпечка", "кг", 1, 1, 350),
    ("garnish", "Гарниры (рис, макароны, картофель)", "кг", 1, 0, 300),
    ("peel", "Очистки и обрезки овощей", "кг", 1, 0, 80),
    ("unsold", "Непроданные блюда", "кг", 1, 1, 900),
    ("expired", "Испорченные и просроченные продукты (в упаковке)", "кг", 0, 0, 700),
    ("dough", "Тесто и брак выпечки", "кг", 1, 0, 300),
    ("product", "Готовая продукция занятий", "кг", 1, 1, 600),
    ("meat", "Мясные обрезки и кости", "кг", 1, 0, 900),
    ("meat_defect", "Брак мясных изделий", "кг", 1, 0, 1800),
    ("whey", "Молочная сыворотка", "л", 1, 0, 50),
    ("dairy_defect", "Брак молочной продукции", "кг", 1, 0, 600),
    ("confect", "Кондитерский брак (крем, обрезки)", "кг", 1, 0, 900),
    ("edu_defect", "Учебный брак блюд", "кг", 1, 0, 800),
    ("breakfast", "Остатки завтраков отеля", "кг", 1, 1, 700),
    ("coffee", "Кофейный жмых", "кг", 1, 0, 0),
    ("milk", "Молоко (тренировки бариста)", "л", 1, 0, 650),
    ("samples", "Образцы после анализов", "кг", 0, 0, 500),
]

# ---------- Организации ----------
# work_days: дни недели, когда точка обязана сдавать отчёт (1=пн ... 7=вс), '' = нерегулярно
# template: {вид отхода: базовый объём в рабочий день}
ORGS = [
    dict(slug="stolovaya-1", name="Столовая главного корпуса", type="canteen", building="Главный корпус",
         work_days="123456",
         template={"plate": 22, "bread": 7, "garnish": 9, "peel": 12, "unsold": 6, "expired": 1.5}),
    dict(slug="stolovaya-2", name="Столовая корпуса 2", type="canteen", building="Корпус 2",
         work_days="12345",
         template={"plate": 12, "bread": 4, "garnish": 5, "peel": 7, "unsold": 3, "expired": 1}),
    dict(slug="bufet", name="Буфет корпуса 407", type="buffet", building="Корпус 407",
         work_days="12345",
         template={"bread": 4, "unsold": 5, "expired": 1.2}),
    dict(slug="atu-inn", name="ATU Inn (ресторан, отель, кофейня)", type="inn", building="ATU Inn",
         work_days="1234567",
         template={"peel": 4, "plate": 4, "edu_defect": 3, "breakfast": 1.5, "coffee": 3, "milk": 2.5,
                   "product": 1.5}),
    dict(slug="lab-hleb", name="Лаборатория хлебопечения", type="edu_lab", building="Корпус 3",
         work_days="135",
         template={"dough": 3.2, "product": 2.5}),
    dict(slug="lab-myaso", name="Лаборатория технологии мяса", type="edu_lab", building="Корпус 3",
         work_days="24",
         template={"meat": 5, "meat_defect": 1.2}),
    dict(slug="lab-moloko", name="Лаборатория молочных продуктов", type="edu_lab", building="Корпус 3",
         work_days="25",
         template={"whey": 18, "dairy_defect": 1.8}),
    dict(slug="lab-konditer", name="Кондитерская лаборатория", type="edu_lab", building="Корпус 3",
         work_days="34",
         template={"confect": 2.5, "product": 2}),
    dict(slug="lab-nauka", name="Научная лаборатория пищевой безопасности", type="sci_lab", building="Корпус 5",
         work_days="",
         template={"samples": 1.0}),
]

LOGINS = {
    "stolovaya-1": ("stol1", "Айгерим Сапарова"),
    "stolovaya-2": ("stol2", "Марина Ким"),
    "bufet": ("bufet", "Динара Ахметова"),
    "atu-inn": ("inn", "Ерлан Нурланов"),
    "lab-hleb": ("lab_hleb", "Гульнара Бекова"),
    "lab-myaso": ("lab_myaso", "Тимур Жаксылыков"),
    "lab-moloko": ("lab_moloko", "Асель Омарова"),
    "lab-konditer": ("lab_konditer", "Сауле Иманова"),
    "lab-nauka": ("lab_nauka", "Арман Сейтказы"),
}

AUTO_COMMENT = "Принято автоматически: все значения в пределах нормы"
RETURN_COMMENTS = [
    "Масса хлеба выглядит завышенной — перепроверьте взвешивание.",
    "Не указаны очистки, хотя кухня работала. Дополните, пожалуйста.",
    "Сыворотку нужно указывать в литрах, а не в килограммах.",
    "Приложите фото контейнера с непроданными блюдами.",
]


def season_factor(d: date, org_type: str) -> float:
    """Летом (до 1 сентября) кампус почти пустой."""
    if d < date(d.year, 9, 1) and d.month in (6, 7, 8):
        # летом кампус почти пустой; в лабораториях — летняя практика и подготовка к семестру
        return {"canteen": 0.3, "buffet": 0.25, "inn": 0.6, "edu_lab": 0.4, "sci_lab": 0.6}.get(org_type, 0.0)
    return 1.0


def daily_qty(org, key, base, d: date) -> float:
    k = season_factor(d, org["type"])
    if k == 0:
        return 0.0
    wd = d.isoweekday()
    m = 1.0
    # закономерности, которые потом «найдут» рекомендации
    if org["slug"] == "stolovaya-1" and key == "bread" and wd == 1:
        m *= 1.7                               # «понедельничный хлеб»
    if org["type"] == "canteen" and wd == 6:
        m *= 0.5                               # суббота — мало студентов
    if org["slug"] == "bufet":
        weeks_to_end = (TODAY - d).days / 7
        if weeks_to_end < 4:
            m *= 1.3                           # рост за последний месяц
    if org["slug"] == "stolovaya-2" and key == "expired" and d == TODAY - timedelta(days=26):
        m *= 9                                 # сломался холодильник
    if org["slug"] == "atu-inn" and key == "milk" and wd == 6:
        m *= 3                                 # суббота — курсы бариста
    if org["slug"] == "atu-inn" and wd in (6, 7) and key == "breakfast":
        m *= 1.8                               # в выходные больше гостей
    noise = random.uniform(0.8, 1.2)
    return round(max(base * k * m * noise, 0), 1)


def is_report_day(org, d: date) -> bool:
    if season_factor(d, org["type"]) == 0:
        return False
    if org["work_days"] == "":
        return random.random() < 0.35          # научная лаборатория — нерегулярно
    return str(d.isoweekday()) in org["work_days"]


def ts(d: date, hh: int, mm: int = 0) -> str:
    return datetime(d.year, d.month, d.day, hh, mm).isoformat(timespec="minutes")


def run(path=None):
    path = path or db.DB_PATH
    if os.path.exists(path):
        os.remove(path)
    conn = db.connect(path)
    db.init_schema(conn)
    cur = conn.cursor()

    wt_ids = {}
    for key, name, unit, rec, edible, price in WASTE_TYPES:
        cur.execute("INSERT INTO waste_types (name, unit, recyclable, edible, price_per_unit) VALUES (?,?,?,?,?)",
                    (name, unit, rec, edible, price))
        wt_ids[key] = cur.lastrowid

    pw = generate_password_hash(DEMO_PASSWORD)
    cur.execute("INSERT INTO users (name, login, password_hash, role) VALUES (?,?,?,?)",
                ("Администратор системы", "admin", pw, "admin"))
    cur.execute("INSERT INTO users (name, login, password_hash, role) VALUES (?,?,?,?)",
                ("Эколог АТУ (проверяющий)", "proverka", pw, "reviewer"))
    reviewer_id = cur.lastrowid
    cur.execute("INSERT INTO users (name, login, password_hash, role) VALUES (?,?,?,?)",
                ("Проректор по АХР", "rukovodstvo", pw, "management"))

    org_ids, resp_ids = {}, {}
    for o in ORGS:
        pin = f"{len(org_ids) + 1}" * 4          # демо: 1111, 2222, … 9999
        cur.execute("INSERT INTO organizations (name, slug, type, building, work_days, pin) VALUES (?,?,?,?,?,?)",
                    (o["name"], o["slug"], o["type"], o["building"], o["work_days"], pin))
        oid = cur.lastrowid
        org_ids[o["slug"]] = oid
        for i, key in enumerate(o["template"]):
            cur.execute("INSERT INTO org_waste_types (org_id, waste_type_id, sort) VALUES (?,?,?)",
                        (oid, wt_ids[key], i))
        login, name = LOGINS[o["slug"]]
        cur.execute("INSERT INTO users (name, login, password_hash, role, org_id) VALUES (?,?,?,?,?)",
                    (name, login, pw, "responsible", oid))
        resp_ids[o["slug"]] = cur.lastrowid

    # Рабочие дни каждой точки и её последний рабочий день до сегодня
    days = {}
    d = START
    while d <= TODAY:
        for o in ORGS:
            if is_report_day(o, d):
                days.setdefault(o["slug"], set()).add(d)
        d += timedelta(days=1)
    last_day = {s: max((x for x in ds if x < TODAY), default=None) for s, ds in days.items()}

    d = START
    while d <= TODAY:
        for o in ORGS:
            if d not in days.get(o["slug"], ()):
                continue
            slug, oid, uid = o["slug"], org_ids[o["slug"]], resp_ids[o["slug"]]
            yesterday = last_day[slug]

            # Свежие дни — разные статусы, чтобы было что показать в демо
            if d == TODAY:
                if slug not in ("stolovaya-1", "lab-hleb", "atu-inn"):
                    continue                    # остальные ещё не начали
                status = "draft"
            elif d == yesterday:
                if slug == "bufet":
                    continue                    # буфет не сдал отчёт — видно в контроле
                # во «Входящих» — только то, что система не приняла сама
                status = {"stolovaya-1": "submitted", "atu-inn": "submitted", "stolovaya-2": "in_review",
                          "lab-moloko": "returned"}.get(slug, "accepted")
            else:
                status = "accepted"

            items = []
            for key, base in o["template"].items():
                q = daily_qty(o, key, base, d)
                if q <= 0:
                    continue
                surplus = 0.0
                # съедобные излишки: часть готовой продукции отдают студентам (с октября — после запуска)
                if key == "product" and d >= TODAY - timedelta(days=35):
                    surplus = round(q * random.uniform(0.5, 0.8), 1)
                items.append((wt_ids[key], q, surplus))
            if not items:
                continue

            note = None
            if d == yesterday and slug == "stolovaya-1":      # всплеск гарнира → ручная проверка
                items = [(w, round(q * 2.6, 1) if w == wt_ids["garnish"] else q, s) for w, q, s in items]
            if d == yesterday and slug == "atu-inn":
                note = "Был банкет на 40 человек, остались закуски"
            cur.execute("INSERT INTO reports (org_id, date, status, note, created_by, updated_at) VALUES (?,?,?,?,?,?)",
                        (oid, d.isoformat(), status, note, uid, ts(d, 17)))
            rid = cur.lastrowid
            for wid, q, s in items:
                cur.execute("INSERT INTO report_items (report_id, waste_type_id, qty, surplus_qty) VALUES (?,?,?,?)",
                            (rid, wid, q, s))

            # История статусов
            if status != "draft":
                cur.execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                            (rid, "submitted", uid, note, ts(d, 17, 30)))
            was_returned = status == "accepted" and random.random() < 0.04
            if was_returned:
                cur.execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                            (rid, "returned", reviewer_id, random.choice(RETURN_COMMENTS), ts(d + timedelta(days=1), 10)))
                cur.execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                            (rid, "submitted", uid, "Исправлено", ts(d + timedelta(days=1), 12)))
            if status == "in_review":
                cur.execute("INSERT INTO status_history (report_id, status, user_id, ts) VALUES (?,?,?,?)",
                            (rid, "in_review", reviewer_id, ts(TODAY, 9, 15)))
            if status == "returned":
                cur.execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                            (rid, "returned", reviewer_id, RETURN_COMMENTS[2], ts(TODAY, 9, 40)))
            if status == "accepted":
                anomaly = slug == "stolovaya-2" and d == TODAY - timedelta(days=26)
                if was_returned or anomaly or random.random() < 0.12:   # проверяющий смотрел вручную
                    cur.execute("INSERT INTO status_history (report_id, status, user_id, ts) VALUES (?,?,?,?)",
                                (rid, "accepted", reviewer_id, ts(d + timedelta(days=1), 15)))
                else:                                        # в пределах нормы — система приняла сама
                    cur.execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                                (rid, "accepted", None, AUTO_COMMENT, ts(d, 17, 31)))

            # Объявления об излишках
            for wid, q, s in items:
                if s > 0 and status != "draft":
                    cur.execute("INSERT INTO surplus_posts (org_id, text, until, ts, sent_to_telegram) VALUES (?,?,?,?,1)",
                                (oid, f"{o['name']} ({o['building']}): {s:g} кг готовой продукции — бесплатно студентам".replace(".", ","),
                                 "16:00", ts(d, 14)))
        d += timedelta(days=1)

    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    kg = conn.execute("SELECT ROUND(SUM(qty)) FROM report_items").fetchone()[0]
    conn.close()
    print(f"Готово: {path}\n  отчётов: {n}, всего отходов: {kg} кг/л, период {START} — {TODAY}")
    print("  PIN точек для входа по QR: 1111 (столовая 1), 2222, … 5555 (хлебопечение), … 9999")
    print(f"  логины: admin, proverka, rukovodstvo, stol1, stol2, bufet, inn, lab_hleb, lab_myaso, "
          f"lab_moloko, lab_konditer, lab_nauka — пароль '{DEMO_PASSWORD}'")


if __name__ == "__main__":
    run()
