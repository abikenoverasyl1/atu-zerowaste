"""Генератор демо-данных по ТЗ трека 1: 5 организаций АТУ, виды отходов — по образцам таблиц учёта,
отчёт = месяц, запись = одно взвешивание (дата, наименование, вес/объём, способ утилизации, ответственный).

Объёмы — ОЦЕНКИ для демонстрации, не реальные данные АТУ.
CO2: 0,627 кг CO2e на кг пищевых отходов на полигоне (UK DEFRA 2022); для упаковки, стекла, пластика,
металла и масла CO2 при захоронении в MVP не считается (коэффициент 0).

Запуск:  python seed.py          (пересоздаёт zerowaste.db)
"""
import os
import random
from datetime import date, datetime, timedelta

from werkzeug.security import generate_password_hash

import db

random.seed(42)
TODAY = date.today()
START = TODAY - timedelta(days=95)   # больше 3 месяцев истории по каждой организации
DEMO_PASSWORD = "demo"

# ---------- Способы переработки / утилизации ----------
# (ключ, название, отходы реально идут в переработку)
METHODS = [
    ("tbo", "Вывоз ТБО", 0),
    ("bak", "Вывоз ТБО (мусорный бак)", 0),
    ("recycle", "Передача на переработку (вторсырьё)", 1),
    ("compost", "Компостирование", 1),
    ("feed", "На корм животным", 1),
    ("oil", "Сдача отработанного масла переработчику", 1),
]

# ---------- Виды отходов по образцам таблиц АТУ ----------
# (ключ, название, ед., пригоден к переработке, съедобный излишок, пищевой, ₸ за ед. сырья — оценка, способ по умолчанию)
WASTE_TYPES = [
    # Кафедра ТПП
    ("pack", "Упаковочные отходы", "кг", 0, 0, 0, 0, "tbo"),
    ("meat_bones", "Мясные кости", "кг", 1, 0, 1, 400, "tbo"),
    ("fish_bones", "Рыбные кости", "кг", 1, 0, 1, 400, "tbo"),
    # Кафедра ТХиПП
    ("egg", "Яичная скорлупа", "кг", 1, 0, 1, 0, "tbo"),
    ("sucrose", "Сахароза", "кг", 1, 0, 1, 500, "tbo"),
    ("pet", "ПЭТ-бутылка 1,0 л (растительное масло)", "кг", 1, 0, 0, 0, "tbo"),
    ("glass", "Стеклобутылка", "кг", 1, 0, 0, 0, "tbo"),
    ("bakery", "Отходы образцов хлебобулочных изделий", "кг", 1, 1, 1, 400, "tbo"),
    ("confect", "Отходы образцов кондитерских изделий", "кг", 1, 1, 1, 900, "tbo"),
    ("fruitveg", "Отходы фруктов и овощей", "кг", 1, 0, 1, 300, "tbo"),
    ("alu", "Алюминиевые банки", "кг", 1, 0, 0, 0, "tbo"),
    # ATU INN и Столовый комплекс
    ("peel", "Очистки овощей", "кг", 1, 0, 1, 80, "bak"),
    ("meat_trim", "Обрезки мясопродуктов", "кг", 1, 0, 1, 900, "bak"),
    ("hall", "Пищевые отходы из зала", "кг", 1, 0, 1, 450, "bak"),
    ("unsold", "Остатки нереализованной продукции", "кг", 1, 1, 1, 900, "bak"),
    ("oil", "Отработанное масло", "л", 1, 0, 0, 700, "tbo"),
    ("carton", "Картонная тара", "кг", 1, 0, 0, 0, "bak"),
    # Кофейня
    ("coffee", "Кофейный жмых", "кг", 1, 0, 1, 0, "bak"),
    ("food_left", "Остатки еды", "кг", 1, 1, 1, 600, "bak"),
]

# ---------- 5 организаций из ТЗ ----------
# work_days: дни недели со взвешиваниями (1=пн … 7=вс); у кафедр — дни лабораторных занятий
# template: {вид отхода: (базовый объём за день, число взвешиваний в день)}
ORGS = [
    dict(slug="iskander", name="Столовый комплекс корп. 1, 2 (ИП «Искандер»)", type="canteen",
         building="Корпуса 1 и 2", work_days="123456", login="iskander", person="Айгерим Сапарова",
         template={"peel": (14, 1), "meat_trim": (4, 1), "hall": (26, 2), "unsold": (8, 1), "oil": (0.8, 1),
                   "carton": (3, 1)}),
    dict(slug="atu-inn", name="Учебно-гостиничный комплекс ATU INN", type="inn",
         building="ATU INN", work_days="1234567", login="inn", person="Ерлан Нурланов",
         template={"peel": (4, 1), "meat_trim": (1.5, 1), "hall": (4.5, 1), "unsold": (2, 1), "oil": (0.3, 1),
                   "carton": (1.5, 1)}),
    dict(slug="canella", name="Кофейня АТУ (ИП «Canella»)", type="cafe",
         building="Главный корпус", work_days="123456", login="canella", person="Динара Ахметова",
         template={"coffee": (3, 1), "carton": (1.4, 1), "food_left": (1.2, 1)}),
    dict(slug="kaf-tpp", name="Кафедра ТПП", type="dept_lab",
         building="Корпус 3", work_days="24", login="kaf_tpp", person="Тимур Жаксылыков",
         template={"pack": (1.5, 1), "meat_bones": (3, 1), "fish_bones": (1.2, 1)}),
    dict(slug="kaf-thipp", name="Кафедра ТХиПП", type="dept_lab",
         building="Корпус 3", work_days="135", login="kaf_thipp", person="Гульнара Бекова",
         template={"egg": (0.6, 1), "sucrose": (0.3, 1), "pet": (0.25, 1), "glass": (0.8, 1), "bakery": (2.2, 1),
                   "confect": (1.4, 1), "fruitveg": (1.2, 1), "alu": (0.2, 1)}),
]

AUTO_COMMENT = "Принято автоматически: все значения в пределах нормы"
RETURN_COMMENTS = [
    "Масса пищевых отходов из зала выглядит завышенной — перепроверьте взвешивание.",
    "Не указаны очистки овощей, хотя кухня работала. Дополните, пожалуйста.",
    "Отработанное масло нужно указывать в литрах, а не в килограммах.",
    "Укажите даты лабораторных занятий для всех записей.",
]


def season_factor(d: date, org_type: str) -> float:
    """Летом кампус почти пустой; на кафедрах — летняя практика."""
    if d.month in (6, 7, 8):
        return {"canteen": 0.3, "cafe": 0.3, "inn": 0.6, "dept_lab": 0.4}.get(org_type, 0.0)
    return 1.0


def daily_qty(org, key, base, d: date) -> float:
    k = season_factor(d, org["type"])
    wd = d.isoweekday()
    m = 1.0
    # закономерности, которые потом «найдут» рекомендации и аналитика
    if org["slug"] == "iskander" and key == "unsold" and wd == 1:
        m *= 1.7                                       # по понедельникам остаётся больше
    if org["type"] == "canteen" and wd == 6:
        m *= 0.5                                       # суббота — мало студентов
    if org["slug"] == "iskander" and key == "meat_trim" and d == TODAY - timedelta(days=26):
        m *= 6                                         # сломался холодильник
    if org["slug"] == "atu-inn" and wd in (6, 7) and key in ("hall", "unsold"):
        m *= 1.6                                       # в выходные больше гостей
    if org["slug"] == "canella" and (TODAY - d).days < 28:
        m *= 1.25                                      # рост за последний месяц
    if org["slug"] == "kaf-thipp" and key in ("bakery", "confect") and d.month == 10:
        m *= 1.2                                       # в октябре больше практических занятий
    return round(max(base * k * m * random.uniform(0.8, 1.2), 0), 2)


def ts(d: date, hh: int, mm: int = 0) -> str:
    return datetime(d.year, d.month, d.day, hh, mm).isoformat(timespec="minutes")


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


def run(path=None):
    path = path or db.DB_PATH
    if os.path.exists(path):
        os.remove(path)
    conn = db.connect(path)
    db.init_schema(conn)
    cur = conn.cursor()

    mt_ids = {}
    for key, name, rec in METHODS:
        mt_ids[key] = cur.execute("INSERT INTO disposal_methods (name, recycled) VALUES (?,?)", (name, rec)).lastrowid

    wt_ids, wt_food = {}, {}
    for key, name, unit, rec, edible, food, price, method in WASTE_TYPES:
        wt_ids[key] = cur.execute(
            "INSERT INTO waste_types (name, unit, recyclable, edible, food, price_per_unit, co2_factor, default_method_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (name, unit, rec, edible, food, price, 0.627 if food else 0.0, mt_ids[method])).lastrowid
        wt_food[key] = (method, unit)

    pw = generate_password_hash(DEMO_PASSWORD)
    cur.execute("INSERT INTO users (name, login, email, password_hash, role) VALUES (?,?,?,?,?)",
                ("Администратор системы", "admin", "admin@atu.kz", pw, "admin"))
    reviewer_id = cur.execute("INSERT INTO users (name, login, email, password_hash, role) VALUES (?,?,?,?,?)",
                              ("Эколог АТУ (проверяющий)", "proverka", "eco@atu.kz", pw, "reviewer")).lastrowid
    cur.execute("INSERT INTO users (name, login, email, password_hash, role) VALUES (?,?,?,?,?)",
                ("Проректор по АХР", "rukovodstvo", "ahr@atu.kz", pw, "management"))

    org_ids, resp_ids = {}, {}
    for n, o in enumerate(ORGS, start=1):
        oid = cur.execute("INSERT INTO organizations (name, slug, type, building, work_days, pin) VALUES (?,?,?,?,?,?)",
                          (o["name"], o["slug"], o["type"], o["building"], o["work_days"], str(n) * 4)).lastrowid
        org_ids[o["slug"]] = oid
        for i, key in enumerate(o["template"]):
            cur.execute("INSERT INTO org_waste_types (org_id, waste_type_id, sort) VALUES (?,?,?)",
                        (oid, wt_ids[key], i))
        resp_ids[o["slug"]] = cur.execute(
            "INSERT INTO users (name, login, email, password_hash, role, org_id) VALUES (?,?,?,?,?,?)",
            (o["person"], o["login"], f"{o['login']}@atu.kz", pw, "responsible", oid)).lastrowid

    # Месяцы в периоде
    months = []
    m = month_start(START)
    while m <= TODAY:
        months.append(m)
        m = month_end(m) + timedelta(days=1)
    cur_month = month_start(TODAY)
    last_month = months[-2]

    for o in ORGS:
        slug, oid, uid = o["slug"], org_ids[o["slug"]], resp_ids[o["slug"]]
        for ms in months:
            # Статус отчёта за месяц — чтобы в демо было что показать
            if ms == cur_month:
                status = "draft"                              # текущий месяц ещё заполняется
            elif ms == last_month and slug == "canella":
                status = "submitted"                          # ждёт проверки во «Входящих»
            elif ms == last_month and slug == "kaf-thipp":
                status = "in_review"
            elif ms == last_month and slug == "kaf-tpp":
                status = "returned"                           # вернули, кафедра ещё не исправила → «не сдан»
            else:
                status = "accepted"
            note = "Санитарный день 14.10, кофейня не работала" if (ms == last_month and slug == "canella") else None
            rid = cur.execute("INSERT INTO reports (org_id, date, status, note, created_by, updated_at) "
                              "VALUES (?,?,?,?,?,?)", (oid, ms.isoformat(), status, note, uid,
                                                       ts(min(month_end(ms), TODAY), 18))).lastrowid
            d = max(ms, START)
            last_day = min(month_end(ms), TODAY - timedelta(days=1))   # сегодня — пусто, для живого демо
            while d <= last_day:
                if str(d.isoweekday()) in o["work_days"]:
                    for key, (base, times) in o["template"].items():
                        q = daily_qty(o, key, base, d)
                        if q <= 0:
                            continue
                        method_key = wt_food[key][0]
                        # с октября часть картона и масла сдают в переработку — видно долю «фактически переработано»
                        if key == "carton" and d >= date(d.year, 10, 1) and random.random() < 0.4:
                            method_key = "recycle"
                        if key == "oil" and slug == "atu-inn" and d >= date(d.year, 10, 1):
                            method_key = "oil"
                        if key == "coffee" and random.random() < 0.3:
                            method_key = "compost"
                        parts = [0.7, 0.3] if times == 2 else [1.0]
                        for pi, share in enumerate(parts):
                            qty = round(q * share, 1)
                            if qty <= 0:
                                continue
                            surplus = 0.0
                            # съедобные излишки отдают студентам (с середины октября — после запуска)
                            if key in ("unsold", "food_left", "bakery", "confect") and d >= TODAY - timedelta(days=35):
                                surplus = round(qty * random.uniform(0.3, 0.6), 1)
                            cur.execute("INSERT INTO report_items (report_id, date, waste_type_id, qty, surplus_qty, "
                                        "method_id, created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                                        (rid, d.isoformat(), wt_ids[key], qty, surplus, mt_ids[method_key], uid,
                                         ts(d, 14 + pi * 4, random.randint(0, 59))))
                            if surplus > 0:
                                cur.execute("INSERT INTO surplus_posts (org_id, text, until, ts, sent_to_telegram) "
                                            "VALUES (?,?,?,?,0)",
                                            (oid, f"{o['name']} ({o['building']}): {surplus:g} кг — бесплатно студентам"
                                                  .replace(".", ","), "16:00", ts(d, 14)))
                d += timedelta(days=1)

            # История статусов
            sub_day = min(month_end(ms) + timedelta(days=2), TODAY)
            if status != "draft":
                cur.execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                            (rid, "submitted", uid, note, ts(sub_day, 10)))
            if status == "accepted":
                was_returned = random.random() < 0.15
                if was_returned:
                    cur.execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                                (rid, "returned", reviewer_id, random.choice(RETURN_COMMENTS), ts(sub_day, 15)))
                    cur.execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                                (rid, "submitted", uid, "Исправлено", ts(sub_day + timedelta(days=1), 11)))
                if was_returned or random.random() < 0.4:      # проверяющий смотрел вручную
                    cur.execute("INSERT INTO status_history (report_id, status, user_id, ts) VALUES (?,?,?,?)",
                                (rid, "accepted", reviewer_id, ts(sub_day + timedelta(days=1), 16)))
                else:                                          # в пределах нормы — система приняла сама
                    cur.execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                                (rid, "accepted", None, AUTO_COMMENT, ts(sub_day, 10, 1)))
            if status == "returned":
                cur.execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                            (rid, "returned", reviewer_id, "Не указан способ утилизации для упаковки — уточните по акту",
                             ts(sub_day, 15)))
            if status == "in_review":
                cur.execute("INSERT INTO status_history (report_id, status, user_id, ts) VALUES (?,?,?,?)",
                            (rid, "in_review", reviewer_id, ts(TODAY, 9, 15)))

    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM report_items").fetchone()[0]
    kg = conn.execute("SELECT ROUND(SUM(qty)) FROM report_items").fetchone()[0]
    conn.close()
    print(f"Готово: {path}\n  записей (взвешиваний): {n}, всего {kg} кг/л, период {START} — {TODAY}")
    print("  PIN для входа по QR: 1111 Искандер, 2222 ATU INN, 3333 Canella, 4444 ТПП, 5555 ТХиПП")
    print(f"  логины: admin, proverka, rukovodstvo, iskander, inn, canella, kaf_tpp, kaf_thipp — пароль '{DEMO_PASSWORD}'")


if __name__ == "__main__":
    run()
