"""ATU Zero Waste — цифровая система учёта пищевых отходов университетского комплекса.

Сквозной сценарий: QR на точке → форма со смартфона → проверка → реестр → дашборд → сводный отчёт.
Запуск локально:  python seed.py && python app.py   → http://localhost:5000
"""
import json
import os
import random
import uuid
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import ai
import analytics as an
import db
import exports
from i18n import LANGS, get_lang, gettext as _, js_translations
from notify import send_telegram

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
UPLOAD_DIR = os.path.join(db.BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.teardown_appcontext(db.close_db)

# Если базы нет (например, свежий деплой) — создаём с демо-данными
if not os.path.exists(db.DB_PATH):
    import seed
    seed.run()


# ---------------- helpers ----------------
def today():
    return date.today()


@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = db.get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def login_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if g.user is None:
                return redirect(url_for("login", next=request.path))
            if roles and g.user["role"] not in roles:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


ICONS = [("хлеб", "🍞"), ("выпеч", "🥐"), ("тесто", "🥖"), ("тарел", "🍽️"), ("гарнир", "🍚"), ("очистк", "🥕"),
         ("непрода", "🍲"), ("просроч", "🗑️"), ("готовая", "🧺"), ("мяс", "🥩"), ("сыворот", "🥛"),
         ("молочн", "🧀"), ("молоко", "🥛"), ("кондит", "🧁"), ("учебн", "👩‍🍳"), ("завтрак", "🍳"),
         ("кофе", "☕"), ("образц", "🧪"), ("мука", "🌾")]


def waste_icon(name):
    n = name.lower()
    return next((ic for key, ic in ICONS if key in n), "♻️")


@app.context_processor
def inject():
    pending = 0
    if g.get("user") is not None and g.user["role"] in ("reviewer", "admin"):
        pending = db.get_db().execute(
            "SELECT COUNT(*) FROM reports WHERE status IN ('submitted', 'in_review')").fetchone()[0]
    return dict(ROLES=db.ROLES, STATUSES=db.STATUSES, ORG_TYPES=db.ORG_TYPES, pending_count=pending,
                fmt_money=an.fmt_money, fmt_num=an.fmt_num, fmt_big=lambda x: an.fmt_big(x).replace('тыс.', _('тыс.')), fmt_mass=an.fmt_mass,
                today=today(), icon=waste_icon, timedelta_days=lambda n: timedelta(days=n), ai_on=ai.enabled(), ai_photo_on=ai.can_see_images(), ai_name=ai.name(),
                _=_, lang=get_lang(), LANGS=LANGS, js_t=js_translations())


def now_iso():
    return datetime.now().isoformat(timespec="minutes")


def add_history(report_id, status, comment=None):
    db.get_db().execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                        (report_id, status, g.user["id"], comment, now_iso()))


def org_template(org_id):
    return db.get_db().execute("""
        SELECT w.* FROM org_waste_types ow JOIN waste_types w ON w.id = ow.waste_type_id
        WHERE ow.org_id = ? AND w.active = 1 ORDER BY ow.sort, w.name""", (org_id,)).fetchall()


def can_fill(org_id):
    return g.user["role"] == "admin" or (g.user["role"] == "responsible" and g.user["org_id"] == org_id)


def parse_date(s, default):
    try:
        return date.fromisoformat(s) if s else default
    except ValueError:
        return default


# ---------------- auth ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db.get_db().execute("SELECT * FROM users WHERE login = ?",
                                (request.form.get("login", "").strip(),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            lang = session.get("lang")
            session.clear()
            session["uid"] = u["id"]
            session.permanent = True
            if lang:
                session["lang"] = lang
            nxt = request.args.get("next")
            return redirect(nxt if nxt and nxt.startswith("/") else url_for("index"))
        flash(_("Неверный логин или пароль"), "danger")
    return render_template("login.html")


@app.route("/lang/<code>")
def set_lang(code):
    """Переключатель языка: запоминаем в сессии и в cookie на год."""
    if code in LANGS:
        session["lang"] = code
    back = request.referrer if request.referrer and request.referrer.startswith(request.host_url) else url_for("index")
    resp = redirect(back)
    resp.set_cookie("lang", session.get("lang", "ru"), max_age=365 * 24 * 3600, samesite="Lax")
    return resp


@app.route("/logout")
def logout():
    lang = session.get("lang")
    session.clear()
    if lang:
        session["lang"] = lang
    return redirect(url_for("login"))


@app.route("/")
@login_required()
def index():
    role = g.user["role"]
    if role == "responsible":
        return redirect(url_for("form", org_id=g.user["org_id"], d=today().isoformat()))
    if role == "reviewer":
        return redirect(url_for("review_list"))
    return redirect(url_for("dashboard"))


# ---------------- QR → форма ----------------
AUTO_ACCEPT = os.environ.get("AUTO_ACCEPT", "1") == "1"
HIGH_FACTOR = 1.8          # во сколько раз выше нормы — повод для ручной проверки
LOW_FACTOR = 0.3           # меньше 30% нормы — возможное занижение
SPOT_CHECK = float(os.environ.get("SPOT_CHECK", "0.1"))   # доля нормальных отчётов на случайную проверку
AUTO_COMMENT = "Принято автоматически: все значения в пределах нормы"


@app.route("/f/<slug>", methods=["GET", "POST"])
def qr_entry(slug):
    """QR-код ведёт сюда. Уже вошли — сразу форма. Не вошли — 4-значный PIN точки вместо логина и пароля."""
    conn = db.get_db()
    org = conn.execute("SELECT * FROM organizations WHERE slug = ? AND active = 1", (slug,)).fetchone()
    if not org:
        abort(404)
    if g.user is not None:
        if not can_fill(org["id"]):
            flash(_("Эта форма принадлежит другой точке"), "warning")
            return redirect(url_for("index"))
        return redirect(url_for("form", org_id=org["id"], d=today().isoformat()))
    error = None
    fails = session.get("pin_fails", 0)
    locked_until = session.get("pin_locked_until")
    if locked_until and datetime.fromisoformat(locked_until) > datetime.now():
        error = _("Слишком много попыток. Подождите 5 минут или войдите по логину.")
    elif request.method == "POST":
        if org["pin"] and request.form.get("pin") == org["pin"]:
            u = conn.execute("SELECT * FROM users WHERE org_id = ? AND role = 'responsible' ORDER BY id LIMIT 1",
                             (org["id"],)).fetchone()
            if u:
                lang = session.get("lang")
                session.clear()
                session["uid"] = u["id"]
                session.permanent = True     # телефон на кухне запоминает вход
                if lang:
                    session["lang"] = lang
                return redirect(url_for("form", org_id=org["id"], d=today().isoformat()))
            error = _("К точке не привязан ответственный. Обратитесь к администратору.")
        else:
            fails += 1
            session["pin_fails"] = fails
            if fails >= 5:
                session["pin_locked_until"] = (datetime.now() + timedelta(minutes=5)).isoformat()
                session["pin_fails"] = 0
            error = _("Неверный PIN. Попробуйте ещё раз.")
    return render_template("pin.html", org=org, error=error, solo=True)


# ---------------- месячный отчёт: записи взвешиваний ----------------
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
              "ноября", "декабря"]
MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
              "ноябрь", "декабрь"]


def month_bounds(d):
    start = d.replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end


def month_label(d):
    return f"{_(MONTHS_NOM[d.month - 1])} {d.year}"


def get_report(conn, org_id, d):
    start, _end = month_bounds(d)
    return conn.execute("SELECT * FROM reports WHERE org_id = ? AND date = ?", (org_id, start.isoformat())).fetchone()


def report_items(conn, rid):
    return conn.execute("""
        SELECT i.*, w.name, w.unit, w.edible, m.name AS method, u.name AS user_name
        FROM report_items i JOIN waste_types w ON w.id = i.waste_type_id
        LEFT JOIN disposal_methods m ON m.id = i.method_id LEFT JOIN users u ON u.id = i.created_by
        WHERE i.report_id = ? ORDER BY i.date DESC, i.created_at DESC, i.id DESC""", (rid,)).fetchall()


def disposal_methods():
    return db.get_db().execute("SELECT * FROM disposal_methods ORDER BY id").fetchall()


def analyze_report(conn, report):
    """Сравнивает месяц с нормой точки. Норма = среднее за рабочий день по принятым отчётам за 90 дней
    до этого месяца (без него самого). Возвращает (причины для ручной проверки, сводка по видам отходов)."""
    org_id = report["org_id"]
    m_start = date.fromisoformat(report["date"])
    prev_from = (m_start - timedelta(days=90)).isoformat()
    prev = conn.execute("""
        SELECT i.waste_type_id, SUM(i.qty) AS total, COUNT(DISTINCT i.date) AS days, AVG(i.qty) AS per_entry
        FROM report_items i JOIN reports r ON r.id = i.report_id
        WHERE r.org_id = ? AND r.status = 'accepted' AND r.date < ? AND i.date >= ?
        GROUP BY i.waste_type_id""", (org_id, m_start.isoformat(), prev_from)).fetchall()
    prev_days = conn.execute("""
        SELECT COUNT(DISTINCT i.date) FROM report_items i JOIN reports r ON r.id = i.report_id
        WHERE r.org_id = ? AND r.status = 'accepted' AND r.date < ? AND i.date >= ?""",
        (org_id, m_start.isoformat(), prev_from)).fetchone()[0]
    norm = {p["waste_type_id"]: p for p in prev}
    items = report_items(conn, report["id"])
    cur_days = len({i["date"] for i in items}) or 1
    by_type = {}
    for i in items:
        t = by_type.setdefault(i["waste_type_id"], dict(name=i["name"], unit=i["unit"], total=0.0, surplus=0.0,
                                                         n=0, max_entry=None))
        t["total"] += i["qty"]
        t["surplus"] += i["surplus_qty"]
        t["n"] += 1
        if t["max_entry"] is None or i["qty"] > t["max_entry"]["qty"]:
            t["max_entry"] = i
    reasons, summary = [], []
    if prev_days < 10:
        reasons.append(_("мало истории по точке для сравнения"))
    for wid, t in by_type.items():
        nm = _(t["name"]).lower()
        p = norm.get(wid)
        expected = (p["total"] / prev_days * cur_days) if (p and prev_days) else None
        flag = None
        if prev_days >= 10:
            if not p or p["days"] < 5:
                reasons.append(_("новый вид отхода «{nm}» — нет истории", nm=nm))
                flag = "new"
            else:
                daily, daily_norm = t["total"] / cur_days, p["total"] / prev_days
                if daily > daily_norm * HIGH_FACTOR:
                    reasons.append(_("«{nm}» в {k} раза больше обычного", nm=nm, k=an.fmt_num(daily / daily_norm)))
                    flag = "high"
                elif daily_norm >= 0.3 and daily < daily_norm * LOW_FACTOR:
                    reasons.append(_("«{nm}» в {k} раза меньше обычного", nm=nm, k=an.fmt_num(daily_norm / daily)))
                    flag = "low"
                me = t["max_entry"]
                if me and p["per_entry"] and me["qty"] > p["per_entry"] * 3 and me["qty"] >= 2:
                    reasons.append(_("запись {d}: «{nm}» {q} {u} — в {k} раза больше обычной записи",
                                     d=f"{me['date'][8:10]}.{me['date'][5:7]}", nm=nm, q=an.fmt_num(me["qty"]),
                                     u=_(t["unit"]), k=an.fmt_num(me["qty"] / p["per_entry"])))
                    flag = flag or "spike"
        summary.append(dict(id=wid, name=t["name"], unit=t["unit"], total=t["total"], surplus=t["surplus"], n=t["n"],
                            expected=expected, flag=flag))
    if prev_days >= 10 and cur_days >= 5:
        for wid, p in norm.items():
            if wid not in by_type and p["days"] / prev_days >= 0.8 and p["total"] / prev_days >= 0.5:
                w = conn.execute("SELECT name, unit FROM waste_types WHERE id = ?", (wid,)).fetchone()
                reasons.append(_("не указано «{nm}», хотя обычно около {avg} {unit} в день", nm=_(w["name"]).lower(),
                                 avg=an.fmt_num(p["total"] / prev_days), unit=_(w["unit"])))
    if report["note"]:
        reasons.append(_("есть комментарий"))
    if conn.execute("SELECT 1 FROM status_history WHERE report_id = ? AND status = 'returned'",
                    (report["id"],)).fetchone():
        reasons.append(_("исправление после возврата"))
    summary.sort(key=lambda x: -x["total"])
    return reasons, summary


def tile_template(conn, org_id):
    return org_template(org_id)


@app.route("/form/<int:org_id>/<d>", methods=["GET", "POST"])
@login_required("responsible", "admin")
def form(org_id, d):
    """Журнал организации за месяц: записи взвешиваний по дням + отправка отчёта за месяц."""
    conn = db.get_db()
    org = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
    if not org:
        abort(404)
    if not can_fill(org_id):
        abort(403)
    day = parse_date(d, today())
    if day > today():
        return redirect(url_for("form", org_id=org_id, d=today().isoformat()))
    m_start, m_end = month_bounds(day)
    report = get_report(conn, org_id, day)
    editable = report is None or report["status"] in ("draft", "returned")
    template = org_template(org_id)
    names = {w["id"]: w for w in template}
    methods = {m["id"]: m for m in disposal_methods()}
    is_fetch = bool(request.headers.get("X-Fetch"))

    def fail(msg, code=400):
        if is_fetch:
            return {"ok": False, "error": msg}, code
        flash(msg, "danger")
        return redirect(url_for("form", org_id=org_id, d=day.isoformat()))

    if request.method == "POST":
        action = request.form.get("action", "add")
        if not editable:
            return fail(_("Отчёт за месяц уже отправлен, менять его нельзя"), 409)

        if action == "add":
            try:
                wid = int(request.form.get("waste_type_id", "0"))
                q = float((request.form.get("qty") or "0").replace(",", "."))
                s = float((request.form.get("surplus") or "0").replace(",", "."))
                mid = int(request.form.get("method_id") or 0)
            except ValueError:
                return fail(_("Введите число"))
            if wid not in names:
                return fail(_("Выберите вид отхода из списка"))
            w = names[wid]
            if q <= 0 or q > 1000:
                return fail(_("{name}: вес должен быть больше 0 и не больше 1000 {unit}", name=_(w["name"]),
                              unit=_(w["unit"])))
            if s < 0 or s > q:
                return fail(_("{name}: отдать студентам можно не больше, чем взвесили", name=_(w["name"])))
            if mid not in methods:
                mid = w["default_method_id"] or next(iter(methods))
            photo_name = None
            f = request.files.get("photo")
            if f and f.filename:
                ext = os.path.splitext(f.filename)[1].lower()
                if ext in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
                    photo_name = f"{uuid.uuid4().hex}{ext}"
                    f.save(os.path.join(UPLOAD_DIR, photo_name))
            if report is None:
                rid = conn.execute("INSERT INTO reports (org_id, date, status, created_by, updated_at) VALUES (?,?,?,?,?)",
                                   (org_id, m_start.isoformat(), "draft", g.user["id"], now_iso())).lastrowid
            else:
                rid = report["id"]
                conn.execute("UPDATE reports SET updated_at = ? WHERE id = ?", (now_iso(), rid))
            iid = conn.execute("INSERT INTO report_items (report_id, date, waste_type_id, qty, surplus_qty, method_id, "
                               "created_by, created_at, photo) VALUES (?,?,?,?,?,?,?,?,?)",
                               (rid, day.isoformat(), wid, round(q, 2), round(s, 2), mid, g.user["id"], now_iso(),
                                photo_name)).lastrowid
            tg_ok = False
            if s > 0:   # «Не выбросить, а отдать»: объявление сразу, пока еда свежая
                until = request.form.get("until") or "16:00"
                text = (f"🥐 Бесплатно студентам: {org['name']} ({org['building']}).\n"
                        f"{w['name'].lower()} — {an.fmt_num(s)} {w['unit']}.\nЗабрать до {until}.")
                tg_ok = send_telegram(text)
                conn.execute("INSERT INTO surplus_posts (org_id, text, until, ts, sent_to_telegram) VALUES (?,?,?,?,?)",
                             (org_id, text, until, now_iso(), int(tg_ok)))
            conn.commit()
            if is_fetch:
                return {"ok": True, "id": iid, "surplus": s > 0, "tg": tg_ok}
            return redirect(url_for("form", org_id=org_id, d=day.isoformat()))

        if action == "delete":
            if report is None:
                return fail(_("Запись не найдена"), 404)
            conn.execute("DELETE FROM report_items WHERE id = ? AND report_id = ?",
                         (int(request.form.get("item_id", 0)), report["id"]))
            conn.commit()
            if is_fetch:
                return {"ok": True}
            return redirect(url_for("form", org_id=org_id, d=day.isoformat()))

        if action == "note":
            if report is None:
                rid = conn.execute("INSERT INTO reports (org_id, date, status, created_by, updated_at) VALUES (?,?,?,?,?)",
                                   (org_id, m_start.isoformat(), "draft", g.user["id"], now_iso())).lastrowid
            else:
                rid = report["id"]
            conn.execute("UPDATE reports SET note = ? WHERE id = ?", (request.form.get("note", "").strip() or None, rid))
            conn.commit()
            flash(_("Комментарий сохранён"), "info")
            return redirect(url_for("form", org_id=org_id, d=day.isoformat()))

        if action == "submit":
            if report is None or not conn.execute("SELECT 1 FROM report_items WHERE report_id = ?",
                                                  (report["id"],)).fetchone():
                return fail(_("В отчёте за месяц нет ни одной записи"))
            reasons, _summary = analyze_report(conn, report)
            if not reasons and random.random() < SPOT_CHECK:
                reasons.append(_("выборочная проверка (случайно отбирается каждый 10-й отчёт)"))
            status = "accepted" if (AUTO_ACCEPT and not reasons) else "submitted"
            conn.execute("UPDATE reports SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), report["id"]))
            add_history(report["id"], "submitted", report["note"] or None)
            if status == "accepted":
                conn.execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                             (report["id"], "accepted", None, AUTO_COMMENT, now_iso()))
            conn.commit()
            total = conn.execute("SELECT COALESCE(SUM(qty), 0) FROM report_items WHERE report_id = ?",
                                 (report["id"],)).fetchone()[0]
            return redirect(url_for("form", org_id=org_id, d=day.isoformat(), done=1,
                                    auto=1 if status == "accepted" else None, why="; ".join(reasons) or None,
                                    total=round(total, 1)))
        return fail(_("Неизвестное действие"))

    items = report_items(conn, report["id"]) if report else []
    day_items = [i for i in items if i["date"] == day.isoformat()]
    day_by_type, month_by_type = {}, {}
    for i in items:
        month_by_type[i["waste_type_id"]] = month_by_type.get(i["waste_type_id"], 0) + i["qty"]
        if i["date"] == day.isoformat():
            day_by_type[i["waste_type_id"]] = day_by_type.get(i["waste_type_id"], 0) + i["qty"]
    by_day = {}
    for i in items:
        by_day[i["date"]] = by_day.get(i["date"], 0) + i["qty"]
    history = conn.execute("""SELECT h.*, u.name AS user_name FROM status_history h
                              LEFT JOIN users u ON u.id = h.user_id WHERE report_id = ? ORDER BY h.id DESC""",
                           (report["id"],)).fetchall() if report else []
    return render_template(
        "form.html", org=org, day=day, report=report, template=template, editable=editable, history=history,
        day_items=day_items, day_by_type=day_by_type, month_by_type=month_by_type, by_day=sorted(by_day.items(), reverse=True),
        day_total=sum(day_by_type.values()), month_total=sum(month_by_type.values()), n_items=len(items),
        methods=list(methods.values()), month=month_label(day), m_start=m_start,
        prev_day=(day - timedelta(days=1)).isoformat(), next_day=(day + timedelta(days=1)).isoformat(),
        done=request.args.get("done") == "1", auto=request.args.get("auto") == "1", why=request.args.get("why"),
        total=request.args.get("total"))


@app.route("/history")
@login_required("responsible")
def history():
    conn = db.get_db()
    rows = conn.execute("""
        SELECT r.*, COALESCE(SUM(i.qty), 0) AS total, COUNT(i.id) AS n,
               (SELECT comment FROM status_history h WHERE h.report_id = r.id AND h.comment IS NOT NULL
                ORDER BY h.id DESC LIMIT 1) AS last_comment
        FROM reports r LEFT JOIN report_items i ON i.report_id = r.id
        WHERE r.org_id = ? GROUP BY r.id ORDER BY r.date DESC LIMIT 24""", (g.user["org_id"],)).fetchall()
    org = conn.execute("SELECT * FROM organizations WHERE id = ?", (g.user["org_id"],)).fetchone()
    rows = [dict(r, label=month_label(date.fromisoformat(r["date"]))) for r in rows]
    return render_template("history.html", rows=rows, org=org)


@app.route("/org/<int:org_id>/waste-type", methods=["POST"])
@login_required("responsible", "admin", "reviewer")
def add_waste_type(org_id):
    """Новый вид отхода прямо из формы: сохраняется в справочник и в шаблон организации."""
    if g.user["role"] != "reviewer" and not can_fill(org_id):
        abort(403)
    conn = db.get_db()
    name = request.form.get("name", "").strip()
    unit = request.form.get("unit", "кг")
    method = request.form.get("method_id")
    if name:
        existing = conn.execute("SELECT id FROM waste_types WHERE lower(name) = lower(?)", (name,)).fetchone()
        mid = int(method) if method and method.isdigit() else conn.execute(
            "SELECT id FROM disposal_methods ORDER BY id LIMIT 1").fetchone()[0]
        wid = existing["id"] if existing else conn.execute(
            "INSERT INTO waste_types (name, unit, recyclable, food, price_per_unit, default_method_id) "
            "VALUES (?,?,1,1,300,?)", (name, unit if unit in ("кг", "л") else "кг", mid)).lastrowid
        conn.execute("INSERT OR IGNORE INTO org_waste_types (org_id, waste_type_id, sort) VALUES (?,?,99)",
                     (org_id, wid))
        conn.commit()
        flash(_("Вид отхода «{name}» добавлен в форму", name=name), "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/uploads/<name>")
@login_required()
def uploaded(name):
    return send_from_directory(UPLOAD_DIR, name)


# ---------------- проверка ----------------
def report_card(conn, r):
    reasons, summary = analyze_report(conn, r)
    return dict(r=r, label=month_label(date.fromisoformat(r["date"])), reasons=reasons, summary=summary)


@app.route("/review")
@login_required("reviewer", "admin")
def review_list():
    conn = db.get_db()
    rows = conn.execute("""
        SELECT r.*, o.name AS org, COALESCE(SUM(i.qty),0) AS total, COUNT(i.id) AS n
        FROM reports r JOIN organizations o ON o.id = r.org_id
        LEFT JOIN report_items i ON i.report_id = r.id
        WHERE r.status IN ('submitted', 'in_review', 'returned')
        GROUP BY r.id ORDER BY CASE r.status WHEN 'submitted' THEN 0 WHEN 'in_review' THEN 1 ELSE 2 END, r.date DESC
    """).fetchall()
    cards = [report_card(conn, r) for r in rows]
    active = [c for c in cards if c["r"]["status"] != "returned"]
    returned = [c for c in cards if c["r"]["status"] == "returned"]
    auto_rows = conn.execute("""
        SELECT r.*, o.name AS org, COALESCE(SUM(i.qty), 0) AS total, COUNT(i.id) AS n FROM reports r
        JOIN organizations o ON o.id = r.org_id LEFT JOIN report_items i ON i.report_id = r.id
        WHERE r.status = 'accepted' AND r.date >= ?
          AND (SELECT user_id FROM status_history h WHERE h.report_id = r.id AND h.status = 'accepted'
               ORDER BY h.id DESC LIMIT 1) IS NULL
          AND EXISTS (SELECT 1 FROM status_history h WHERE h.report_id = r.id AND h.status = 'accepted')
        GROUP BY r.id ORDER BY r.date DESC, o.name""", ((today() - timedelta(days=100)).isoformat(),)).fetchall()
    auto = [report_card(conn, r) for r in auto_rows]
    tab = request.args.get("tab", "inbox")
    return render_template("review_list.html", active=active, returned=returned, auto=auto,
                           periods=an.period_status(conn, today()),
                           tab=tab if tab in ("inbox", "auto") else "inbox",
                           rules=dict(high=HIGH_FACTOR, low=LOW_FACTOR, spot=round(SPOT_CHECK * 100)))


def is_auto(conn, rid):
    """Последнее «принято» поставила система, а не человек."""
    h = conn.execute("SELECT user_id FROM status_history WHERE report_id = ? AND status = 'accepted' "
                     "ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
    return h is not None and h["user_id"] is None


@app.route("/review/<int:rid>", methods=["GET", "POST"])
@login_required("reviewer", "admin")
def review(rid):
    conn = db.get_db()
    q = ("SELECT r.*, o.name AS org, o.building FROM reports r JOIN organizations o ON o.id = r.org_id "
         "WHERE r.id = ?")
    report = conn.execute(q, (rid,)).fetchone()
    if not report:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        comment = request.form.get("comment", "").strip()
        is_fetch = bool(request.headers.get("X-Fetch"))
        if action == "return" and not comment:
            if is_fetch:
                return ("", 400)
            flash(_("При возврате нужен комментарий — что исправить"), "danger")
            return redirect(request.url)
        auto_acc = report["status"] == "accepted" and is_auto(conn, rid)
        if report["status"] not in ("submitted", "in_review") and not (action == "return" and auto_acc):
            flash(_("Этот отчёт уже обработан"), "warning")
            return redirect(request.url)
        if action in ("accept", "return"):
            st = "accepted" if action == "accept" else "returned"
            conn.execute("UPDATE reports SET status = ?, updated_at = ? WHERE id = ?", (st, now_iso(), rid))
            add_history(rid, st, comment or None)
            conn.commit()
            if is_fetch:
                return ("", 204)
            if auto_acc:
                flash(_("Автопринятый отчёт возвращён на исправление"), "warning")
                return redirect(url_for("review_list", tab="auto"))
            flash(_("Отчёт принят и попал в реестр") if st == "accepted" else _("Отчёт возвращён с комментарием"),
                  "success" if st == "accepted" else "warning")
            return redirect(url_for("review_list"))
        return redirect(request.url)
    if report["status"] == "submitted":
        conn.execute("UPDATE reports SET status = 'in_review' WHERE id = ?", (rid,))
        add_history(rid, "in_review")
        conn.commit()
        report = conn.execute(q, (rid,)).fetchone()
    reasons, summary = analyze_report(conn, report)
    items = report_items(conn, rid)
    history = conn.execute("""SELECT h.*, u.name AS user_name FROM status_history h LEFT JOIN users u
                              ON u.id = h.user_id WHERE report_id = ? ORDER BY h.id DESC""", (rid,)).fetchall()
    photos = [i for i in items if i["photo"]]
    return render_template("review.html", report=report, items=items, summary=summary, reasons=reasons,
                           history=history, photos=photos, label=month_label(date.fromisoformat(report["date"])),
                           total=sum(i["qty"] for i in items),
                           auto=report["status"] == "accepted" and is_auto(conn, rid))


# ---------------- реестр ----------------
def filters_from_request(default_days=30):
    d_to = parse_date(request.args.get("to"), today())
    d_from = parse_date(request.args.get("from"), d_to - timedelta(days=default_days))
    return d_from, d_to, request.args.get("org") or None, request.args.get("wt") or None


REG_STATUSES = {"": "Все записи", "accepted": "Только принятые отчёты"}


@app.route("/registry")
@login_required("reviewer", "admin", "management")
def registry():
    conn = db.get_db()
    d_from, d_to, org, wt = filters_from_request()
    method = request.args.get("method") or None
    st = request.args.get("st", "")
    rows = an.fetch_items(conn, d_from, d_to, org, wt, statuses=("accepted",) if st == "accepted" else None,
                          method_id=method)
    if request.args.get("export") == "xlsx":
        return Response(exports.registry_xlsx(rows),
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename=reestr_{d_from}_{d_to}.xlsx"})
    orgs = conn.execute("SELECT id, name FROM organizations ORDER BY name").fetchall()
    wts = conn.execute("SELECT id, name FROM waste_types ORDER BY name").fetchall()
    return render_template("registry.html", rows=rows[:500], n=len(rows), t=an.totals(rows), orgs=orgs, wts=wts,
                           methods=disposal_methods(), REG_STATUSES=REG_STATUSES,
                           f=dict(frm=d_from, to=d_to, org=org, wt=wt, method=method, st=st))


# ---------------- сводный отчёт ----------------
PERIODS = {"month": "Месяц", "quarter": "Квартал", "year": "Год"}
MONTHS = MONTHS_NOM


@app.route("/report")
@login_required("reviewer", "admin", "management")
def report():
    conn = db.get_db()
    kind = request.args.get("period", "quarter")
    if kind not in PERIODS:
        kind = "quarter"
    ref = parse_date(request.args.get("ref"), today())
    start, end = an.period_bounds(kind, ref)
    label = {"month": f"{_(MONTHS[start.month - 1])} {start.year}",
             "quarter": _("{q} квартал {y}", q=(start.month - 1) // 3 + 1, y=start.year),
             "year": _("{y} год", y=start.year)}[kind] + f" ({start:%d.%m.%Y} — {end:%d.%m.%Y})"
    rows = an.fetch_items(conn, start, end)
    title = _("Сводный отчёт по пищевым отходам АО «АТУ»")
    fmt = request.args.get("export")
    if fmt == "xlsx":
        return Response(exports.summary_xlsx(rows, title, label),
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename=otchet_{kind}_{start}.xlsx"})
    if fmt == "pdf":
        return Response(exports.summary_pdf(rows, title, label), mimetype="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename=otchet_{kind}_{start}.pdf"})
    orgs, types, pivot, by_org = exports.summary_data(rows)
    by_org = sorted(by_org.items(), key=lambda kv: -kv[1]["money"])
    prev_ref = (start - timedelta(days=1)).isoformat()
    next_ref = (end + timedelta(days=1)).isoformat() if end < today() else None
    return render_template("report.html", kind=kind, ref=ref, label=label, t=an.totals(rows), by_org=by_org,
                           prev_ref=prev_ref, next_ref=next_ref,
                           by_type=an.group_sum(rows, "wt"), PERIODS=PERIODS, assumptions=an.ASSUMPTIONS)


# ---------------- дашборд ----------------
@app.route("/dashboard")
@login_required("reviewer", "admin", "management")
def dashboard():
    conn = db.get_db()
    d_from, d_to, _org, _wt = filters_from_request(default_days=90)
    rows = an.fetch_items(conn, d_from, d_to)
    t = an.totals(rows)
    days = max((d_to - d_from).days, 1)
    months, series = an.monthly_series(rows, d_from, d_to, by="org")
    by_org = an.group_sum(rows, "org")
    by_type = an.group_sum(rows, "wt")
    top_types = dict(list(by_type.items())[:7])
    other = sum(list(by_type.values())[7:])
    if other:
        top_types["Прочее"] = other
    by_method = an.group_sum(rows, "method")
    charts = dict(
        monthly=dict(labels=[f"{_(MONTHS_NOM[int(m[5:]) - 1])} {m[:4]}" for m in months],
                     datasets=[dict(label=_(k), data=v) for k, v in sorted(series.items(), key=lambda kv: -sum(kv[1]))]),
        org=dict(labels=[_(k) for k in by_org], data=[round(v, 1) for v in by_org.values()]),
        types=dict(labels=[_(k) for k in top_types], data=[round(v, 1) for v in top_types.values()]),
        methods=dict(labels=[_(k or "—") for k in by_method], data=[round(v, 1) for v in by_method.values()]),
    )
    # Сравнение «месяц к месяцу» за одинаковое число дней (ФТ-6.2)
    td = today()
    cur_from = td.replace(day=1)
    prev_from = (cur_from - timedelta(days=1)).replace(day=1)
    n = (td - cur_from).days
    prev_to = min(prev_from + timedelta(days=n), cur_from - timedelta(days=1))
    cur = an.totals(an.fetch_items(conn, cur_from, td))
    prev = an.totals(an.fetch_items(conn, prev_from, prev_to))
    mom = dict(cur=cur["waste"], prev=prev["waste"], days=n + 1,
               pct=((cur["waste"] / prev["waste"] - 1) * 100) if prev["waste"] else None,
               cur_label=month_label(cur_from), prev_label=month_label(prev_from))
    surplus = conn.execute("""SELECT s.*, o.name AS org FROM surplus_posts s JOIN organizations o ON o.id = s.org_id
                              ORDER BY s.id DESC LIMIT 5""").fetchall()
    span = {30: _("За 30 дней"), 90: _("За 3 месяца"), 365: _("За год")}.get(
        days, _("С {a} по {b}", a=f"{d_from:%d.%m}", b=f"{d_to:%d.%m}"))
    return render_template("dashboard.html", t=t, per_month=lambda x: x / days * 30, charts=json.dumps(charts),
                           span=span, days=days, mom=mom,
                           recs=an.recommendations(conn, today(), tr=_), missing=an.missing_reports(conn, today()),
                           surplus=surplus, f=dict(frm=d_from, to=d_to), assumptions=an.ASSUMPTIONS)


# ---------------- ИИ-помощник ----------------
@app.route("/ai/photo/<int:org_id>", methods=["POST"])
@login_required("responsible", "admin")
def ai_photo(org_id):
    """Фото контейнера → ИИ подсказывает вид отхода и замечает мусор (пластик, упаковку)."""
    if not can_fill(org_id):
        abort(403)
    if not ai.can_see_images():
        return {"ok": False, "error": _("Распознавание фото не подключено: нужен ключ GEMINI_API_KEY (бесплатно) или ANTHROPIC_API_KEY")}, 503
    f = request.files.get("photo")
    if not f:
        return {"ok": False, "error": _("Нет фото")}, 400
    data = f.read()
    media = f.mimetype if f.mimetype in ("image/jpeg", "image/png", "image/webp", "image/gif") else "image/jpeg"
    if len(data) > 4_500_000:
        return {"ok": False, "error": _("Фото слишком большое, сделайте снимок поменьше")}, 400
    options = [dict(id=w["id"], name=w["name"]) for w in org_template(org_id)]
    res = ai.classify_photo(data, media, options)
    if not res:
        return {"ok": False, "error": _("Не удалось распознать. Выберите вид отхода вручную.")}, 502
    return {"ok": True, **res}


@app.route("/ai/summary")
@login_required("reviewer", "admin", "management")
def ai_summary():
    ctx = an.ai_context(db.get_db(), today())
    text = ai.summary(ctx, get_lang()) if ai.enabled() else None
    if not text:
        text = ai.fallback_summary(an.ai_context(db.get_db(), today(), tr=_), tr=_)
    ai_ok = ai.enabled() and bool(text)
    if not text:
        text = ai.fallback_summary(an.ai_context(db.get_db(), today(), tr=_), tr=_)
    note = "" if ai_ok else (_("Сводка собрана по правилам. С ключом ИИ она будет подробнее.") if not ai.enabled()
                             else _("ИИ не ответил ({e}), поэтому сводка собрана по правилам.", e=ai.last_error or "—"))
    return {"ok": True, "ai": ai_ok, "text": text, "note": note}


@app.route("/ai/ask", methods=["POST"])
@login_required("reviewer", "admin", "management")
def ai_ask():
    q = (request.get_json(silent=True) or {}).get("q", "").strip()
    if not q:
        return {"ok": False, "error": _("Напишите вопрос")}, 400
    if not ai.enabled():
        return {"ok": False, "error": _("Вопросы к ИИ работают после подключения ключа: GEMINI_API_KEY или GROQ_API_KEY "
                                        "(оба бесплатные). Сводка выше собрана без ИИ, по правилам.")}, 503
    text = ai.answer(q, an.ai_context(db.get_db(), today()), get_lang())
    if not text:
        err = _("ИИ сейчас не отвечает. Попробуйте ещё раз через минуту.")
        if ai.last_error:
            err += f" ({ai.name()} {ai.last_error})"
        return {"ok": False, "error": err}, 502
    return {"ok": True, "text": text}


# ---------------- излишки (публичная страница) ----------------
@app.route("/surplus")
def surplus():
    posts = db.get_db().execute("""SELECT s.*, o.name AS org, o.building FROM surplus_posts s
                                   JOIN organizations o ON o.id = s.org_id ORDER BY s.id DESC LIMIT 30""").fetchall()
    return render_template("surplus.html", posts=posts)


# ---------------- администрирование ----------------
@app.route("/admin", methods=["GET", "POST"])
@login_required("admin")
def admin():
    conn = db.get_db()
    if request.method == "POST":
        what = request.form.get("what")
        try:
            if what == "org":
                slug = request.form["slug"].strip().lower()
                oid = conn.execute("INSERT INTO organizations (name, slug, type, building, work_days, pin) "
                                   "VALUES (?,?,?,?,?,?)", (request.form["name"].strip(), slug, request.form["type"],
                                                            request.form.get("building", ""),
                                                            request.form.get("work_days", "12345"),
                                                            request.form.get("pin") or None)).lastrowid
                for wid in request.form.getlist("wt"):
                    conn.execute("INSERT INTO org_waste_types (org_id, waste_type_id) VALUES (?,?)", (oid, int(wid)))
                flash(_("Организация добавлена"), "success")
            elif what == "pin":
                pin = request.form.get("pin", "").strip()
                if pin and not (pin.isdigit() and len(pin) == 4):
                    raise ValueError("PIN — ровно 4 цифры")
                conn.execute("UPDATE organizations SET pin = ? WHERE id = ?", (pin or None, int(request.form["org_id"])))
                flash(_("PIN сохранён"), "success")
            elif what == "org_active":
                conn.execute("UPDATE organizations SET active = 1 - active WHERE id = ?", (int(request.form["org_id"]),))
                flash(_("Статус организации изменён"), "success")
            elif what == "method":
                name = request.form.get("name", "").strip()
                if not name:
                    raise ValueError(_("Введите название способа"))
                rec = int("recycled" in request.form)
                co2 = request.form.get("co2", "").replace(",", ".").strip()
                conn.execute("INSERT INTO disposal_methods (name, recycled, landfill, co2_factor, rank) VALUES (?,?,?,?,?)",
                             (name, rec, 1 - rec, float(co2) / 1000 if co2 else None, 3 if rec else 5))
                flash(_("Способ утилизации добавлен"), "success")
            elif what == "user":
                conn.execute("INSERT INTO users (name, login, email, password_hash, role, org_id) VALUES (?,?,?,?,?,?)",
                             (request.form["name"].strip(), request.form["login"].strip(),
                              request.form.get("email", "").strip() or None,
                              generate_password_hash(request.form["password"]), request.form["role"],
                              request.form.get("org_id") or None))
                flash(_("Пользователь добавлен"), "success")
            elif what == "wt":
                for w in conn.execute("SELECT id FROM waste_types").fetchall():
                    i = w["id"]
                    if f"price_{i}" in request.form:
                        conn.execute("UPDATE waste_types SET price_per_unit = ?, recyclable = ?, edible = ?, active = ?, "
                                     "default_method_id = ? WHERE id = ?",
                                     (float(request.form[f"price_{i}"] or 0), int(f"rec_{i}" in request.form),
                                      int(f"ed_{i}" in request.form), int(f"act_{i}" in request.form),
                                      int(request.form.get(f"m_{i}") or 0) or None, i))
                flash(_("Справочник сохранён"), "success")
            conn.commit()
        except Exception as e:  # noqa: BLE001 — показываем ошибку пользователю (например, дубль логина)
            conn.rollback()
            flash(_("Ошибка: {e}", e=e), "danger")
        return redirect(url_for("admin"))
    orgs = conn.execute("""SELECT o.*, (SELECT GROUP_CONCAT(w.name, '|') FROM org_waste_types ow
                           JOIN waste_types w ON w.id = ow.waste_type_id WHERE ow.org_id = o.id) AS types
                           FROM organizations o ORDER BY o.id""").fetchall()
    users = conn.execute("""SELECT u.*, o.name AS org FROM users u LEFT JOIN organizations o ON o.id = u.org_id
                            ORDER BY u.role, u.name""").fetchall()
    wts = conn.execute("""SELECT w.*, (SELECT COUNT(*) FROM report_items i WHERE i.waste_type_id = w.id) AS used,
                          (SELECT name FROM disposal_methods m WHERE m.id = w.rec_method_id) AS rec_name
                          FROM waste_types w ORDER BY w.active DESC, w.name""").fetchall()
    methods = conn.execute("""SELECT m.*, (SELECT COUNT(*) FROM report_items i WHERE i.method_id = m.id) AS used
                              FROM disposal_methods m ORDER BY m.rank, m.id""").fetchall()
    return render_template("admin.html", orgs=orgs, users=users, wts=wts, methods=methods)


@app.route("/qr")
@login_required("admin", "reviewer")
def qr_codes():
    orgs = db.get_db().execute("SELECT * FROM organizations WHERE active = 1 ORDER BY id").fetchall()
    return render_template("qr.html", orgs=orgs, base=request.host_url.rstrip("/"))


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", msg="Нет доступа к этой странице"), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", msg="Страница не найдена"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
