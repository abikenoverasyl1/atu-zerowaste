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
        WHERE ow.org_id = ? ORDER BY ow.sort, w.name""", (org_id,)).fetchall()


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
    org = conn.execute("SELECT * FROM organizations WHERE slug = ?", (slug,)).fetchone()
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


def norm_flags(conn, org_id, values, names, report_day):
    """Правила автоприёмки. Возвращает причины, по которым отчёт должен посмотреть человек.
    Норма = средний вес вида отхода у этой точки по принятым отчётам за 45 дней, БЕЗ последней недели
    (медленный рост не «подтягивает» норму сразу), с учётом дня недели, если по нему хватает данных."""
    d_to = (today() - timedelta(days=7)).isoformat()
    d_from = (today() - timedelta(days=45)).isoformat()
    norms = {r["waste_type_id"]: (r["a"], r["n"]) for r in conn.execute("""
        SELECT i.waste_type_id, AVG(i.qty) AS a, COUNT(*) AS n FROM report_items i JOIN reports r ON r.id = i.report_id
        WHERE r.org_id = ? AND r.status = 'accepted' AND r.date BETWEEN ? AND ? GROUP BY i.waste_type_id""",
        (org_id, d_from, d_to))}
    # Норма с учётом дня недели: если по этому дню недели есть 3+ записи, берём её
    # (по субботам курсы бариста, по понедельникам больше хлеба — это не ошибка, а закономерность)
    wd = str(int(report_day.strftime("%w")))
    for r in conn.execute("""
        SELECT i.waste_type_id, AVG(i.qty) AS a, COUNT(*) AS n FROM report_items i JOIN reports r ON r.id = i.report_id
        WHERE r.org_id = ? AND r.status = 'accepted' AND r.date BETWEEN ? AND ? AND strftime('%w', r.date) = ?
        GROUP BY i.waste_type_id""", (org_id, d_from, d_to, wd)):
        if r["n"] >= 3 and r["waste_type_id"] in norms:
            norms[r["waste_type_id"]] = (r["a"], norms[r["waste_type_id"]][1])
    n_reports = conn.execute("""SELECT COUNT(*) FROM reports WHERE org_id = ? AND status = 'accepted'
                                AND date BETWEEN ? AND ? AND id IN (SELECT report_id FROM report_items)""",
                             (org_id, d_from, d_to)).fetchone()[0]
    reasons = []
    for wid, (q, _s) in values.items():
        nm = _(names[wid]["name"]).lower()
        avg, n = norms.get(wid, (None, 0))
        if q <= 0:
            # вид отхода, который у точки бывает почти всегда, вдруг не указан — возможное занижение
            if n_reports >= 5 and n / n_reports >= 0.8 and avg and avg >= 1:
                reasons.append(_("не указано «{nm}», хотя обычно около {avg} {unit}", nm=nm, avg=an.fmt_num(avg),
                                 unit=_(names[wid]['unit'])))
            continue
        if n < 5:
            reasons.append(_("мало истории по «{nm}»", nm=nm))
        elif q > avg * HIGH_FACTOR:
            reasons.append(_("«{nm}» в {k} раза больше обычного", nm=nm, k=an.fmt_num(q / avg)))
        elif avg >= 1 and q < avg * LOW_FACTOR:
            reasons.append(_("«{nm}» в {k} раза меньше обычного", nm=nm, k=an.fmt_num(avg / q)))
    if not reasons and random.random() < SPOT_CHECK:
        reasons.append(_("выборочная проверка (случайно отбирается каждый 10-й отчёт)"))
    return reasons


@app.route("/form/<int:org_id>/<d>", methods=["GET", "POST"])
@login_required("responsible", "admin")
def form(org_id, d):
    conn = db.get_db()
    org = conn.execute("SELECT * FROM organizations WHERE id = ?", (org_id,)).fetchone()
    if not org:
        abort(404)
    if not can_fill(org_id):
        abort(403)
    day = parse_date(d, today())
    if day > today():
        return redirect(url_for("form", org_id=org_id, d=today().isoformat()))
    report = conn.execute("SELECT * FROM reports WHERE org_id = ? AND date = ?", (org_id, day.isoformat())).fetchone()
    editable = report is None or report["status"] in ("draft", "returned")
    template = org_template(org_id)
    names = {w["id"]: w for w in template}
    is_fetch = bool(request.headers.get("X-Fetch"))

    if request.method == "POST":
        if not editable:
            if is_fetch:
                return {"ok": False, "error": _("Отчёт уже отправлен")}, 409
            flash(_("Отчёт уже отправлен, менять его нельзя"), "warning")
            return redirect(request.url)
        action = request.form.get("action", "draft")
        values, errors = {}, []
        if action != "nowaste":
            for w in template:
                try:
                    q = float((request.form.get(f"qty_{w['id']}") or "0").replace(",", "."))
                    s = float((request.form.get(f"surplus_{w['id']}") or "0").replace(",", "."))
                except ValueError:
                    errors.append(_("{name}: введите число", name=_(w['name']))); continue
                if q < 0 or q > 1000:
                    errors.append(_("{name}: вес должен быть от 0 до 1000 {unit}", name=_(w['name']), unit=_(w['unit'])))
                if s < 0 or s > q:
                    errors.append(_("{name}: отдать студентам можно не больше, чем взвесили", name=_(w['name'])))
                values[w["id"]] = (round(q, 2), round(s, 2))
        if action == "submit" and not any(q > 0 for q, _ in values.values()):
            errors.append(_("Введите вес хотя бы одного вида отходов или нажмите «Сегодня отходов не было»"))
        if errors:
            if is_fetch:
                return {"ok": False, "error": errors[0]}, 400
            for e in errors:
                flash(e, "danger")
            return redirect(request.url)

        photo_name = None
        f = request.files.get("photo")
        if f and f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
                photo_name = f"{uuid.uuid4().hex}{ext}"
                f.save(os.path.join(UPLOAD_DIR, photo_name))

        note = request.form.get("note", "").strip()
        if action == "nowaste":
            note = note or "Отходов не было"
        was_returned = report is not None and report["status"] == "returned"
        reasons = []
        if action in ("submit", "nowaste"):
            status = "submitted"
            if action == "submit":
                reasons = norm_flags(conn, org_id, values, names, day)
                if note:
                    reasons.append(_("есть комментарий"))
                if was_returned:
                    reasons.append(_("исправление после возврата"))
            if AUTO_ACCEPT and not reasons:
                status = "accepted"
        else:
            status = "draft"

        if report is None:
            rid = conn.execute("INSERT INTO reports (org_id, date, status, note, created_by, updated_at) "
                               "VALUES (?,?,?,?,?,?)",
                               (org_id, day.isoformat(), status, note, g.user["id"], now_iso())).lastrowid
        else:
            rid = report["id"]
            old_photo = conn.execute("SELECT photo FROM report_items WHERE report_id = ? AND photo IS NOT NULL "
                                     "LIMIT 1", (rid,)).fetchone()
            photo_name = photo_name or (old_photo["photo"] if old_photo else None)
            conn.execute("UPDATE reports SET status = ?, note = ?, updated_at = ? WHERE id = ?",
                         (status, note, now_iso(), rid))
            conn.execute("DELETE FROM report_items WHERE report_id = ?", (rid,))
        first = True
        for wid, (q, s) in values.items():
            if q > 0:
                conn.execute("INSERT INTO report_items (report_id, waste_type_id, qty, surplus_qty, photo) "
                             "VALUES (?,?,?,?,?)", (rid, wid, q, s, photo_name if first else None))
                first = False

        if status == "draft":
            conn.commit()
            if is_fetch:                       # тихое автосохранение после каждого взвешивания
                return {"ok": True, "saved": now_iso()[11:16]}
            flash(_("Черновик сохранён. Можно вернуться и дописать позже."), "info")
            return redirect(url_for("form", org_id=org_id, d=day.isoformat()))

        add_history(rid, "submitted", note or None)
        if status == "accepted":
            conn.execute("INSERT INTO status_history (report_id, status, user_id, comment, ts) VALUES (?,?,?,?,?)",
                         (rid, "accepted", None, AUTO_COMMENT, now_iso()))
        # «Не выбросить, а отдать»: объявление об излишках
        parts = [f"{names[wid]['name'].lower()} — {an.fmt_num(s)} {names[wid]['unit']}"
                 for wid, (q, s) in values.items() if s > 0]
        parts_ui = [f"{_(names[wid]['name']).lower()} — {an.fmt_num(s)} {_(names[wid]['unit'])}"
                    for wid, (q, s) in values.items() if s > 0]
        if parts:
            until = request.form.get("until") or "16:00"
            text = (f"🥐 Бесплатно студентам: {org['name']} ({org['building']}).\n"
                    + "; ".join(parts) + f".\nЗабрать до {until}.")
            ok = send_telegram(text)
            conn.execute("INSERT INTO surplus_posts (org_id, text, until, ts, sent_to_telegram) VALUES (?,?,?,?,?)",
                         (org_id, text, until, now_iso(), int(ok)))
        conn.commit()
        return redirect(url_for("form", org_id=org_id, d=day.isoformat(), done=1,
                                auto=1 if status == "accepted" else None, nowaste=1 if action == "nowaste" else None,
                                why="; ".join(reasons) or None, gift=", ".join(parts_ui) if parts_ui else None))

    items = {}
    photo = None
    if report:
        for it in conn.execute("SELECT * FROM report_items WHERE report_id = ?", (report["id"],)):
            items[it["waste_type_id"]] = it
            photo = photo or it["photo"]
    history = conn.execute("""SELECT h.*, u.name AS user_name FROM status_history h
                              LEFT JOIN users u ON u.id = h.user_id WHERE report_id = ? ORDER BY h.id DESC""",
                           (report["id"],)).fetchall() if report else []
    month_start = day.replace(day=1)
    month_total = conn.execute("""SELECT COALESCE(SUM(i.qty), 0) FROM report_items i JOIN reports r ON r.id = i.report_id
                                  WHERE r.org_id = ? AND r.date BETWEEN ? AND ? AND r.status != 'draft'""",
                               (org_id, month_start.isoformat(), day.isoformat())).fetchone()[0]
    recent = conn.execute("""SELECT r.date, r.status, COALESCE(SUM(i.qty),0) AS total FROM reports r
                             LEFT JOIN report_items i ON i.report_id = r.id WHERE r.org_id = ?
                             GROUP BY r.id ORDER BY r.date DESC LIMIT 10""", (org_id,)).fetchall()
    day_total = sum(it["qty"] for it in items.values())
    return render_template("form.html", org=org, day=day, report=report, template=template, items=items,
                           editable=editable, history=history, month_total=month_total, recent=recent,
                           photo=photo, prev_day=(day - timedelta(days=1)).isoformat(),
                           next_day=(day + timedelta(days=1)).isoformat(), day_total=day_total,
                           done=request.args.get("done") == "1", gift=request.args.get("gift"),
                           auto=request.args.get("auto") == "1", nowaste=request.args.get("nowaste") == "1",
                           why=request.args.get("why"))


@app.route("/history")
@login_required("responsible")
def history():
    conn = db.get_db()
    rows = conn.execute("""
        SELECT r.*, COALESCE(SUM(i.qty), 0) AS total,
               (SELECT comment FROM status_history h WHERE h.report_id = r.id AND h.comment IS NOT NULL
                ORDER BY h.id DESC LIMIT 1) AS last_comment
        FROM reports r LEFT JOIN report_items i ON i.report_id = r.id
        WHERE r.org_id = ? GROUP BY r.id ORDER BY r.date DESC LIMIT 60""", (g.user["org_id"],)).fetchall()
    org = conn.execute("SELECT * FROM organizations WHERE id = ?", (g.user["org_id"],)).fetchone()
    month = sum(r["total"] for r in rows if r["date"][:7] == today().isoformat()[:7] and r["status"] != "draft")
    return render_template("history.html", rows=rows, org=org, month=month)


@app.route("/org/<int:org_id>/waste-type", methods=["POST"])
@login_required("responsible", "admin")
def add_waste_type(org_id):
    if not can_fill(org_id):
        abort(403)
    conn = db.get_db()
    name = request.form.get("name", "").strip()
    unit = request.form.get("unit", "кг")
    if name:
        existing = conn.execute("SELECT id FROM waste_types WHERE lower(name) = lower(?)", (name,)).fetchone()
        wid = existing["id"] if existing else conn.execute(
            "INSERT INTO waste_types (name, unit, recyclable, price_per_unit) VALUES (?,?,1,300)",
            (name, unit if unit in ("кг", "л") else "кг")).lastrowid
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
    # Позиции каждого отчёта + сравнение с нормой точки за 30 дней — чтобы принимать прямо из списка
    norms = {(r["org_id"], r["waste_type_id"]): r["a"] for r in conn.execute("""
        SELECT r.org_id, i.waste_type_id, AVG(i.qty) AS a FROM report_items i JOIN reports r ON r.id = i.report_id
        WHERE r.status = 'accepted' AND r.date >= ? GROUP BY r.org_id, i.waste_type_id""",
        ((today() - timedelta(days=30)).isoformat(),))}
    cards = []
    for r in rows:
        items = conn.execute("""SELECT i.*, w.name, w.unit FROM report_items i JOIN waste_types w ON w.id = i.waste_type_id
                                WHERE report_id = ? ORDER BY i.qty DESC""", (r["id"],)).fetchall()
        flagged = []
        for i in items:
            norm = norms.get((r["org_id"], i["waste_type_id"]))
            flagged.append(dict(name=i["name"], unit=i["unit"], qty=i["qty"], surplus=i["surplus_qty"],
                                norm=norm, high=bool(norm and i["qty"] > norm * 1.8)))
        cards.append(dict(r=r, items=flagged, high=any(x["high"] for x in flagged)))
    active = [c for c in cards if c["r"]["status"] != "returned"]
    returned = [c for c in cards if c["r"]["status"] == "returned"]
    missing = an.missing_reports(conn, today())

    # Принятые системой за 7 дней — проверяющий может просмотреть и вернуть любой
    auto_rows = conn.execute("""
        SELECT r.*, o.name AS org, COALESCE(SUM(i.qty), 0) AS total FROM reports r
        JOIN organizations o ON o.id = r.org_id LEFT JOIN report_items i ON i.report_id = r.id
        WHERE r.status = 'accepted' AND r.date >= ?
          AND (SELECT user_id FROM status_history h WHERE h.report_id = r.id AND h.status = 'accepted'
               ORDER BY h.id DESC LIMIT 1) IS NULL
          AND EXISTS (SELECT 1 FROM status_history h WHERE h.report_id = r.id AND h.status = 'accepted')
        GROUP BY r.id ORDER BY r.date DESC, o.name""", ((today() - timedelta(days=7)).isoformat(),)).fetchall()
    wd_norms = {(r["org_id"], r["waste_type_id"], r["wd"]): r["a"] for r in conn.execute("""
        SELECT r.org_id, i.waste_type_id, strftime('%w', r.date) AS wd, AVG(i.qty) AS a, COUNT(*) AS n
        FROM report_items i JOIN reports r ON r.id = i.report_id
        WHERE r.status = 'accepted' AND r.date >= ? GROUP BY r.org_id, i.waste_type_id, wd HAVING n >= 3""",
        ((today() - timedelta(days=45)).isoformat(),))}
    auto = []
    for r in auto_rows:
        wd = date.fromisoformat(r["date"]).strftime("%w")
        items = conn.execute("""SELECT i.*, w.name, w.unit FROM report_items i JOIN waste_types w ON w.id = i.waste_type_id
                                WHERE report_id = ? ORDER BY i.qty DESC""", (r["id"],)).fetchall()
        auto.append(dict(r=r, wd=int(wd), items=[dict(
            name=i["name"], unit=i["unit"], qty=i["qty"], surplus=i["surplus_qty"],
            norm=wd_norms.get((r["org_id"], i["waste_type_id"], wd)) or norms.get((r["org_id"], i["waste_type_id"])),
            by_wd=(r["org_id"], i["waste_type_id"], wd) in wd_norms) for i in items]))
    tab = request.args.get("tab", "inbox")
    return render_template("review_list.html", active=active, returned=returned, missing=missing, auto=auto,
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
    report = conn.execute("SELECT r.*, o.name AS org, o.building FROM reports r JOIN organizations o "
                          "ON o.id = r.org_id WHERE r.id = ?", (rid,)).fetchone()
    if not report:
        abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        comment = request.form.get("comment", "").strip()
        if action == "return" and report["status"] == "accepted" and is_auto(conn, rid):
            if not comment:
                if request.headers.get("X-Fetch"):
                    return ("", 400)
                flash(_("При возврате нужен комментарий — что исправить"), "danger")
                return redirect(request.url)
            conn.execute("UPDATE reports SET status = 'returned', updated_at = ? WHERE id = ?", (now_iso(), rid))
            add_history(rid, "returned", comment)
            conn.commit()
            if request.headers.get("X-Fetch"):
                return ("", 204)
            flash(_("Автопринятый отчёт возвращён на исправление"), "warning")
            return redirect(url_for("review_list", tab="auto"))
        if report["status"] not in ("submitted", "in_review"):
            flash(_("Этот отчёт уже обработан"), "warning")
        elif action == "return" and not comment:
            flash(_("При возврате нужен комментарий — что исправить"), "danger")
            return redirect(request.url)
        elif action in ("accept", "return"):
            st = "accepted" if action == "accept" else "returned"
            conn.execute("UPDATE reports SET status = ?, updated_at = ? WHERE id = ?", (st, now_iso(), rid))
            add_history(rid, st, comment or None)
            conn.commit()
            if request.headers.get("X-Fetch"):
                return ("", 204)
            flash(_("Отчёт принят и попал в реестр") if st == "accepted" else _("Отчёт возвращён с комментарием"),
                  "success" if st == "accepted" else "warning")
            return redirect(url_for("review_list"))
        return redirect(request.url)
    if report["status"] == "submitted":
        conn.execute("UPDATE reports SET status = 'in_review' WHERE id = ?", (rid,))
        add_history(rid, "in_review")
        conn.commit()
        report = conn.execute("SELECT r.*, o.name AS org, o.building FROM reports r JOIN organizations o "
                              "ON o.id = r.org_id WHERE r.id = ?", (rid,)).fetchone()
    items = conn.execute("""SELECT i.*, w.name, w.unit FROM report_items i JOIN waste_types w ON w.id = i.waste_type_id
                            WHERE report_id = ? ORDER BY w.name""", (rid,)).fetchall()
    # Сравнение с нормой: среднее по этой точке за 30 дней
    avg = {r["waste_type_id"]: r["a"] for r in conn.execute("""
        SELECT i.waste_type_id, AVG(i.qty) AS a FROM report_items i JOIN reports r ON r.id = i.report_id
        WHERE r.org_id = ? AND r.status = 'accepted' AND r.date >= ? GROUP BY i.waste_type_id""",
        (report["org_id"], (today() - timedelta(days=30)).isoformat()))}
    history = conn.execute("""SELECT h.*, u.name AS user_name FROM status_history h LEFT JOIN users u
                              ON u.id = h.user_id WHERE report_id = ? ORDER BY h.id DESC""", (rid,)).fetchall()
    photo = next((i["photo"] for i in items if i["photo"]), None)
    return render_template("review.html", report=report, items=items, avg=avg, history=history, photo=photo,
                           auto=report["status"] == "accepted" and is_auto(conn, rid))


# ---------------- реестр ----------------
def filters_from_request(default_days=30):
    d_to = parse_date(request.args.get("to"), today())
    d_from = parse_date(request.args.get("from"), d_to - timedelta(days=default_days))
    return d_from, d_to, request.args.get("org") or None, request.args.get("wt") or None


@app.route("/registry")
@login_required("reviewer", "admin", "management")
def registry():
    conn = db.get_db()
    d_from, d_to, org, wt = filters_from_request()
    rows = an.fetch_items(conn, d_from, d_to, org, wt)
    if request.args.get("export") == "xlsx":
        return Response(exports.registry_xlsx(rows),
                        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename=reestr_{d_from}_{d_to}.xlsx"})
    orgs = conn.execute("SELECT id, name FROM organizations ORDER BY name").fetchall()
    wts = conn.execute("SELECT id, name FROM waste_types ORDER BY name").fetchall()
    return render_template("registry.html", rows=rows[:500], n=len(rows), t=an.totals(rows), orgs=orgs, wts=wts,
                           f=dict(frm=d_from, to=d_to, org=org, wt=wt))


# ---------------- сводный отчёт ----------------
PERIODS = {"month": "Месяц", "quarter": "Квартал", "year": "Год"}
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
          "ноябрь", "декабрь"]


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
    labels, series = an.weekly_series(rows, d_from, d_to, by="org_type")
    by_org_money = an.group_sum(rows, "org", "money")
    by_type = an.group_sum(rows, "wt")
    top_types = dict(list(by_type.items())[:7])
    other = sum(list(by_type.values())[7:])
    if other:
        top_types["Прочее"] = other
    charts = dict(
        weekly=dict(labels=labels, datasets=[dict(label=_(db.ORG_TYPES.get(k, k)), data=v) for k, v in series.items()]),
        org=dict(labels=[_(k) for k in by_org_money], data=[round(v) for v in by_org_money.values()]),
        types=dict(labels=[_(k) for k in top_types], data=[round(v, 1) for v in top_types.values()]),
    )
    surplus = conn.execute("""SELECT s.*, o.name AS org FROM surplus_posts s JOIN organizations o ON o.id = s.org_id
                              ORDER BY s.id DESC LIMIT 5""").fetchall()
    span = {30: _("За 30 дней"), 90: _("За 3 месяца"), 365: _("За год")}.get(
        days, _("С {a} по {b}", a=f"{d_from:%d.%m}", b=f"{d_to:%d.%m}"))
    return render_template("dashboard.html", t=t, per_month=lambda x: x / days * 30, charts=json.dumps(charts),
                           span=span, days=days,
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
    return {"ok": True, "ai": ai.enabled() and bool(text), "text": text,
            "note": "" if ai.enabled() else _("Сводка собрана по правилам. С ключом ИИ она будет подробнее.")}


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
        return {"ok": False, "error": _("ИИ сейчас не отвечает. Попробуйте ещё раз через минуту.")}, 502
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
            elif what == "user":
                conn.execute("INSERT INTO users (name, login, password_hash, role, org_id) VALUES (?,?,?,?,?)",
                             (request.form["name"].strip(), request.form["login"].strip(),
                              generate_password_hash(request.form["password"]), request.form["role"],
                              request.form.get("org_id") or None))
                flash(_("Пользователь добавлен"), "success")
            elif what == "wt":
                for w in conn.execute("SELECT id FROM waste_types").fetchall():
                    i = w["id"]
                    if f"price_{i}" in request.form:
                        conn.execute("UPDATE waste_types SET price_per_unit = ?, recyclable = ?, edible = ? WHERE id = ?",
                                     (float(request.form[f"price_{i}"] or 0), int(f"rec_{i}" in request.form),
                                      int(f"ed_{i}" in request.form), i))
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
    wts = conn.execute("SELECT * FROM waste_types ORDER BY name").fetchall()
    return render_template("admin.html", orgs=orgs, users=users, wts=wts)


@app.route("/qr")
@login_required("admin", "reviewer")
def qr_codes():
    orgs = db.get_db().execute("SELECT * FROM organizations ORDER BY id").fetchall()
    return render_template("qr.html", orgs=orgs, base=request.host_url.rstrip("/"))


@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", msg="Нет доступа к этой странице"), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", msg="Страница не найдена"), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
