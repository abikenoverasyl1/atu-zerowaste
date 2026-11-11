"""Аналитика: деньги, CO2, графики, рекомендации, контроль сдачи отчётов.

Допущения (выводятся в интерфейсе, чтобы жюри видело источник каждой цифры):
- CO2: 0,627 кг CO2e на 1 кг пищевых отходов на полигоне (UK DEFRA conversion factors 2022).
- Вывоз ТБО в Алматы: 3 832,5 ₸ за м³ (тариф с 28.06.2026, решение маслихата № 308).
- Плотность пищевых отходов в контейнере: ~400 кг/м³ (допущение).
- Стоимость сырья по видам отходов — оценка команды, правится в справочнике.
"""
from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, pstdev

DISPOSAL_COST_PER_M3 = 3832.5
WASTE_DENSITY_KG_M3 = 400
DISPOSAL_COST_PER_KG = DISPOSAL_COST_PER_M3 / WASTE_DENSITY_KG_M3   # ≈ 9,6 ₸/кг

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_DAT = ["понедельникам", "вторникам", "средам", "четвергам", "пятницам", "субботам", "воскресеньям"]
DEFECT_WORDS = ("брак", "тесто")

ASSUMPTIONS = [
    "CO₂: 0,627 кг CO₂e на 1 кг пищевых отходов на полигоне — UK DEFRA, 2022",
    "Вывоз ТБО: 3 832,5 ₸/м³ — тариф Алматы с 28.06.2026; плотность отходов ~400 кг/м³ (допущение)",
    "Стоимость сырья по видам отходов — оценка команды, редактируется в справочнике",
    "1 л жидких отходов ≈ 1 кг",
]


def fetch_items(db, date_from=None, date_to=None, org_id=None, waste_type_id=None, statuses=("accepted",)):
    sql = """
        SELECT r.id AS report_id, r.date, r.status, o.id AS org_id, o.name AS org, o.type AS org_type,
               w.id AS wt_id, w.name AS wt, w.unit, w.recyclable, w.price_per_unit, w.co2_factor,
               i.qty, i.surplus_qty
        FROM report_items i
        JOIN reports r ON r.id = i.report_id
        JOIN organizations o ON o.id = r.org_id
        JOIN waste_types w ON w.id = i.waste_type_id
        WHERE r.status IN ({})
    """.format(",".join("?" * len(statuses)))
    args = list(statuses)
    if date_from:
        sql += " AND r.date >= ?"; args.append(str(date_from))
    if date_to:
        sql += " AND r.date <= ?"; args.append(str(date_to))
    if org_id:
        sql += " AND o.id = ?"; args.append(int(org_id))
    if waste_type_id:
        sql += " AND w.id = ?"; args.append(int(waste_type_id))
    sql += " ORDER BY r.date DESC, o.name, w.name"
    rows = [dict(r) for r in db.execute(sql, args).fetchall()]
    for r in rows:
        enrich(r)
    return rows


def enrich(r):
    """Добавляет к строке массу отходов, деньги и CO2."""
    waste = max(r["qty"] - r["surplus_qty"], 0)
    r["waste"] = waste
    r["money"] = waste * (r["price_per_unit"] + DISPOSAL_COST_PER_KG)
    r["co2"] = waste * r["co2_factor"]
    r["saved_money"] = r["surplus_qty"] * (r["price_per_unit"] + DISPOSAL_COST_PER_KG)
    r["saved_co2"] = r["surplus_qty"] * r["co2_factor"]
    return r


def totals(rows):
    t = dict(waste=0.0, money=0.0, co2=0.0, surplus=0.0, saved_money=0.0, saved_co2=0.0, recyclable=0.0)
    for r in rows:
        t["waste"] += r["waste"]
        t["money"] += r["money"]
        t["co2"] += r["co2"]
        t["surplus"] += r["surplus_qty"]
        t["saved_money"] += r["saved_money"]
        t["saved_co2"] += r["saved_co2"]
        if r["recyclable"]:
            t["recyclable"] += r["waste"]
    t["recyclable_share"] = (t["recyclable"] / t["waste"] * 100) if t["waste"] else 0
    return t


def group_sum(rows, key, field="waste"):
    acc = defaultdict(float)
    for r in rows:
        acc[r[key]] += r[field]
    return dict(sorted(acc.items(), key=lambda kv: -kv[1]))


def weekly_series(rows, date_from, date_to, by="org_type"):
    """Сумма отходов по неделям (понедельник недели) с разбивкой."""
    weeks = []
    d = date_from - timedelta(days=date_from.weekday())
    while d <= date_to:
        weeks.append(d)
        d += timedelta(days=7)
    idx = {w.isoformat(): i for i, w in enumerate(weeks)}
    series = defaultdict(lambda: [0.0] * len(weeks))
    for r in rows:
        dd = date.fromisoformat(r["date"])
        wk = (dd - timedelta(days=dd.weekday())).isoformat()
        if wk in idx:
            series[r[by]][idx[wk]] += r["waste"]
    labels = [w.strftime("%d.%m") for w in weeks]
    return labels, {k: [round(x, 1) for x in v] for k, v in series.items()}


def period_bounds(kind: str, ref: date):
    if kind == "month":
        start = ref.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    elif kind == "quarter":
        q = (ref.month - 1) // 3
        start = date(ref.year, q * 3 + 1, 1)
        end = (date(ref.year + (q == 3), (q * 3 + 3) % 12 + 1, 1)) - timedelta(days=1)
    else:
        start, end = date(ref.year, 1, 1), date(ref.year, 12, 31)
    return start, end


def missing_reports(db, today: date, days_back=7):
    """Какие точки не сдали отчёт за рабочий день (черновик тоже считается несданным)."""
    orgs = db.execute("SELECT id, name, work_days FROM organizations WHERE work_days != ''").fetchall()
    have = defaultdict(set)
    for r in db.execute("SELECT org_id, date FROM reports WHERE status != 'draft' AND date >= ?",
                        ((today - timedelta(days=days_back)).isoformat(),)):
        have[r["org_id"]].add(r["date"])
    out = []
    for o in orgs:
        for k in range(1, days_back + 1):
            d = today - timedelta(days=k)
            if str(d.isoweekday()) in o["work_days"] and d.isoformat() not in have[o["id"]]:
                # Летом лаборатории закрыты — не считаем
                out.append(dict(org=o["name"], org_id=o["id"], date=d.isoformat()))
    return sorted(out, key=lambda x: x["date"], reverse=True)


def recommendations(db, today: date, tr=None):
    """Правила, которые превращают данные в действия. Каждая рекомендация оценена в ₸/мес.
    tr — функция перевода (по умолчанию русский текст)."""
    tr = tr or (lambda s, **kw: s.format(**kw) if kw else s)
    rows = fetch_items(db, date_from=today - timedelta(days=60), date_to=today)
    recs = []

    # Ряды по (организация, вид отхода) → по дням
    daily = defaultdict(dict)
    meta = {}
    for r in rows:
        key = (r["org"], r["wt"])
        daily[key][r["date"]] = daily[key].get(r["date"], 0) + r["waste"]
        meta[key] = r

    # 1. Перекос по дням недели
    for key, series in daily.items():
        if len(series) < 8:
            continue
        vals = list(series.values())
        avg = mean(vals)
        by_wd = defaultdict(list)
        for ds, v in series.items():
            by_wd[date.fromisoformat(ds).weekday()].append(v)
        for wd, vs in by_wd.items():
            if len(vs) >= 3 and avg > 0.5:
                ratio = mean(vs) / avg
                if ratio >= 1.4:
                    m = meta[key]
                    excess = (mean(vs) - avg) * 4.3  # ~4,3 таких дня в месяце
                    recs.append(dict(
                        kind="Закупки",
                        title=tr("{org}: {wt} по {wd} ×{k}", org=tr(key[0]), wt=tr(key[1]).lower(),
                                 wd=tr(WEEKDAYS_DAT[wd]), k=fmt_num(ratio)),
                        text=tr("По {wd} отходов «{wt}» на {p}% больше среднего. Уменьшите закупку или выпуск "
                                "на этот день недели.", wd=tr(WEEKDAYS_DAT[wd]), wt=tr(key[1]).lower(),
                                p=f"{(ratio - 1) * 100:.0f}"),
                        impact=excess * (m["price_per_unit"] + DISPOSAL_COST_PER_KG)))

    # 2. Аномальные дни (последние 45 дней)
    for key, series in daily.items():
        if len(series) < 10:
            continue
        vals = list(series.values())
        avg, sd = mean(vals), pstdev(vals)
        for ds, v in series.items():
            if date.fromisoformat(ds) >= today - timedelta(days=45) and v > avg + 4 * sd and v > 2.5 * avg:
                m = meta[key]
                recs.append(dict(
                    kind="Аномалия",
                    title=tr("{org}: всплеск {d}", org=tr(key[0]), d=f"{ds[8:10]}.{ds[5:7]}"),
                    text=tr("«{wt}»: {v} {u} при норме ~{n}. Проверьте холодильное оборудование и условия "
                            "хранения (ХАССП), зафиксируйте причину.", wt=tr(key[1]), v=fmt_num(v), u=tr(m["unit"]),
                            n=fmt_num(avg)),
                    impact=(v - avg) * (m["price_per_unit"] + DISPOSAL_COST_PER_KG)))

    # 3. Рост за последние 4 недели
    by_org = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        d = date.fromisoformat(r["date"])
        if d > today - timedelta(days=28):
            by_org[r["org"]][1] += r["money"]
        elif d > today - timedelta(days=56):
            by_org[r["org"]][0] += r["money"]
    for org, (prev, last) in by_org.items():
        if prev > 0 and last / prev >= 1.15:
            recs.append(dict(
                kind="Тренд",
                title=tr("{org}: рост потерь +{p}%", org=tr(org), p=f"{(last / prev - 1) * 100:.0f}"),
                text=tr("За последние 4 недели потери выросли относительно предыдущих 4. "
                     "Сверьте ассортимент и объём выпуска с фактическими продажами."),
                impact=last - prev))

    # 4. Доля брака в учебных лабораториях
    lab = defaultdict(lambda: [0.0, 0.0, 0.0])  # брак, всего, деньги брака
    for r in rows:
        if r["org_type"] == "edu_lab":
            lab[r["org"]][1] += r["qty"]
            if any(w in r["wt"].lower() for w in DEFECT_WORDS):
                lab[r["org"]][0] += r["qty"]
                lab[r["org"]][2] += r["money"]
    for org, (defect, total, money) in lab.items():
        if total and defect / total >= 0.4 and "сыворот" not in org.lower():
            recs.append(dict(
                kind="Лаборатория",
                title=tr("{org}: доля брака {p}%", org=tr(org), p=f"{defect / total * 100:.0f}"),
                text=tr("Брак — больше 40% всей массы. Рассчитывайте закладку сырья под численность группы, "
                     "проверьте рецептуры и навески; съедобную продукцию отдавайте через «Излишки»."),
                impact=money / 2 * 0.3))  # за 2 месяца → в месяц, сократить на 30%

    # 5. Перерабатываемые отходы уходят на полигон
    rec_kg = sum(r["waste"] for r in rows if r["recyclable"]) / 2
    if rec_kg > 0:
        recs.append(dict(
            kind="Переработка",
            title=tr("{kg} кг/мес можно не везти на полигон", kg=fmt_num(rec_kg, 0)),
            text=tr("Очистки, кофейный жмых, сыворотку и остатки можно передавать на корм или компост "
                 "(договор с фермерским хозяйством). Это снижает вывоз ТБО и выбросы метана."),
            impact=rec_kg * DISPOSAL_COST_PER_KG))

    recs.sort(key=lambda x: -x["impact"])
    # не больше двух рекомендаций одного вида, всего 6
    out, seen = [], defaultdict(int)
    for r in recs:
        if seen[r["kind"]] < 2:
            out.append(r)
            seen[r["kind"]] += 1
        if len(out) == 6:
            break
    return out


def fmt_money(x):
    return f"{x:,.0f}".replace(",", " ") + " ₸"


def fmt_num(x, digits=1):
    """1234.5 → «1 234,5», 3.0 → «3» — по-русски: пробел в тысячах, запятая в дробях."""
    if x is None:
        return "—"
    s = f"{x:,.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(",", " ").replace(".", ",")


def fmt_big(x):
    """3 407 093 → «3,4 млн ₸», 850 000 → «850 тыс. ₸»."""
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f}".replace(".", ",") + " млн ₸"
    if x >= 10_000:
        return f"{x / 1000:.0f} тыс. ₸"
    return fmt_money(x)


def fmt_mass(kg):
    if kg >= 1000:
        return f"{kg / 1000:.1f}".replace(".", ",") + " т"
    return f"{kg:.0f} кг"


MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
             "ноябрь", "декабрь"]


def ai_context(db, today: date, days=90, tr=None):
    """Компактная выжимка данных для ИИ: только агрегаты, без персональных данных."""
    d_from = today - timedelta(days=days)
    rows = fetch_items(db, d_from, today)
    t = totals(rows)
    by_org = defaultdict(lambda: [0.0, 0.0])
    by_type = defaultdict(float)
    by_month = defaultdict(float)
    by_wd = defaultdict(float)
    for r in rows:
        by_org[r["org"]][0] += r["waste"]
        by_org[r["org"]][1] += r["money"]
        by_type[(r["wt"], r["unit"])] += r["waste"]
        by_month[r["date"][:7]] += r["waste"]
        by_wd[date.fromisoformat(r["date"]).weekday()] += r["waste"]
    return {
        "дней": days,
        "период": f"последние {days} дней ({d_from:%d.%m.%Y}–{today:%d.%m.%Y}), только принятые отчёты",
        "итого": {"отходы_кг": round(t["waste"]), "потери_тг": round(t["money"]), "co2_кг": round(t["co2"]),
                  "доля_перерабатываемых_проц": round(t["recyclable_share"]),
                  "отдано_студентам_кг": round(t["surplus"], 1)},
        "по_точкам": [{"точка": k, "кг": round(v[0], 1), "потери_тг": round(v[1])}
                      for k, v in sorted(by_org.items(), key=lambda kv: -kv[1][1])],
        "по_видам": [{"вид": k[0], "ед": k[1], "кг": round(v, 1)}
                     for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])[:12]],
        "по_месяцам": [{"месяц": MONTHS_RU[int(m[5:]) - 1], "кг": round(v)} for m, v in sorted(by_month.items())],
        "полные_месяцы": [{"месяц": MONTHS_RU[int(m[5:]) - 1], "кг": round(v)} for m, v in sorted(by_month.items())
                          if m != today.isoformat()[:7] and m != d_from.isoformat()[:7]],
        "по_дням_недели_кг": {WEEKDAYS[k]: round(v) for k, v in sorted(by_wd.items())},
        "рекомендации": [{"заголовок": r["title"], "совет": r["title"] + ". " + r["text"],
                          "экономия_тг_мес": round(r["impact"])} for r in recommendations(db, today, tr=tr)],
        "не_сдали_отчёт": [f"{m['org']} ({m['date']})" for m in missing_reports(db, today)],
        "допущения": ASSUMPTIONS,
    }
