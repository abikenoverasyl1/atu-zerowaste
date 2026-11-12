"""Аналитика: деньги, CO2, графики, рекомендации, контроль сдачи отчётов.

Допущения (выводятся в интерфейсе, чтобы жюри видело источник каждой цифры):
- CO2: UK DEFRA / BEIS 2024, Waste disposal. CO2 записи = масса × коэффициент способа утилизации;
  для вывоза ТБО — коэффициент полигона для вида отхода (пищевые 700,21, картон 1 164,39, стекло 8,88 кг/т).
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
DEFECT_WORDS = ("брак", "тесто", "образц")

ASSUMPTIONS = [
    "CO₂: коэффициенты UK DEFRA / BEIS 2024 (Waste disposal). Полигон: пищевые 700,21, картон 1 164,39, стекло 8,88 кг CO₂-экв/т; компост и биогаз — 8,88; переработка — 6,41",
    "Экономия CO₂ = масса × (коэффициент полигона − коэффициент выбранного способа). Корм животным — коэффициента DEFRA нет, считаем 0",
    "Допущения: ПЭТ и алюминий — как стекло, масло — как пищевые отходы (1 л ≈ 0,92 кг), смешанная упаковка — как картон. Британские коэффициенты для Казахстана ориентировочные",
    "Вывоз ТБО: 3 832,5 ₸/м³ — тариф Алматы с 28.06.2026; плотность отходов ~400 кг/м³ (допущение)",
    "Стоимость сырья по видам отходов — оценка команды, редактируется в справочнике",
    "Остальные жидкие отходы: 1 л ≈ 1 кг",
]


def fetch_items(db, date_from=None, date_to=None, org_id=None, waste_type_id=None, statuses=None, method_id=None):
    """Строки журнала (одно взвешивание = одна строка). statuses=None — все записи, включая текущий месяц."""
    sql = """
        SELECT i.id, r.id AS report_id, i.date, r.status, o.id AS org_id, o.name AS org, o.type AS org_type,
               w.id AS wt_id, w.name AS wt, w.unit, w.recyclable, w.food, w.price_per_unit, w.co2_factor,
               w.kg_per_unit, w.category, i.qty, i.surplus_qty, m.id AS method_id, m.name AS method,
               COALESCE(m.recycled, 0) AS recycled, COALESCE(m.landfill, 1) AS m_landfill, m.co2_factor AS m_co2,
               rm.name AS rec_method, rm.landfill AS rm_landfill, rm.co2_factor AS rm_co2, u.name AS user_name
        FROM report_items i
        JOIN reports r ON r.id = i.report_id
        JOIN organizations o ON o.id = r.org_id
        JOIN waste_types w ON w.id = i.waste_type_id
        LEFT JOIN disposal_methods m ON m.id = i.method_id
        LEFT JOIN disposal_methods rm ON rm.id = w.rec_method_id
        LEFT JOIN users u ON u.id = i.created_by
        WHERE 1 = 1
    """
    args = []
    if statuses:
        sql += " AND r.status IN ({})".format(",".join("?" * len(statuses))); args += list(statuses)
    if date_from:
        sql += " AND i.date >= ?"; args.append(str(date_from))
    if date_to:
        sql += " AND i.date <= ?"; args.append(str(date_to))
    if org_id:
        sql += " AND o.id = ?"; args.append(int(org_id))
    if waste_type_id:
        sql += " AND w.id = ?"; args.append(int(waste_type_id))
    if method_id:
        sql += " AND m.id = ?"; args.append(int(method_id))
    sql += " ORDER BY i.date DESC, o.name, w.name, i.id DESC"
    rows = [dict(r) for r in db.execute(sql, args).fetchall()]
    for r in rows:
        enrich(r)
    return rows


def enrich(r):
    """Добавляет к строке массу отходов, деньги и CO2."""
    waste = max(r["qty"] - r["surplus_qty"], 0)
    r["waste"] = waste
    landfill = 0 if r.get("recycled") else waste      # переработанное не едет на полигон
    r["landfill"] = landfill
    r["money"] = waste * r["price_per_unit"] + landfill * DISPOSAL_COST_PER_KG
    kg = waste * (r.get("kg_per_unit") or 1)
    base = r["co2_factor"]                              # кг CO2e/кг, если бы везли на полигон
    method_f = base if r.get("m_landfill", 1) else (r.get("m_co2") or 0)
    r["co2"] = kg * method_f                            # фактический CO2 выбранного способа
    r["co2_avoided"] = kg * (base - method_f)           # уже сэкономлено за счёт способа (ФТ: экология)
    if r.get("rec_method") is not None and r.get("m_landfill", 1):
        rec_f = base if r.get("rm_landfill") else (r.get("rm_co2") or 0)
        r["co2_potential"] = kg * max(base - rec_f, 0)  # можно сэкономить, перейдя на рекомендуемый способ
    else:
        r["co2_potential"] = 0.0
    r["saved_money"] = r["surplus_qty"] * (r["price_per_unit"] + DISPOSAL_COST_PER_KG)
    r["saved_co2"] = r["surplus_qty"] * (r.get("kg_per_unit") or 1) * r["co2_factor"]
    return r


def totals(rows):
    t = dict(waste=0.0, money=0.0, co2=0.0, surplus=0.0, saved_money=0.0, saved_co2=0.0, recyclable=0.0,
             recycled=0.0, n=len(rows), co2_avoided=0.0, co2_potential=0.0)
    for r in rows:
        t["co2_avoided"] += r["co2_avoided"]
        t["co2_potential"] += r["co2_potential"]
        if r.get("recycled"):
            t["recycled"] += r["waste"]
        t["waste"] += r["waste"]
        t["money"] += r["money"]
        t["co2"] += r["co2"]
        t["surplus"] += r["surplus_qty"]
        t["saved_money"] += r["saved_money"]
        t["saved_co2"] += r["saved_co2"]
        if r["recyclable"]:
            t["recyclable"] += r["waste"]
    t["recyclable_share"] = (t["recyclable"] / t["waste"] * 100) if t["waste"] else 0
    t["recycled_share"] = (t["recycled"] / t["waste"] * 100) if t["waste"] else 0
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


def monthly_series(rows, date_from, date_to, by="org"):
    """Сумма отходов по месяцам с разбивкой (по организации или виду отхода)."""
    months = []
    d = date_from.replace(day=1)
    while d <= date_to:
        months.append(d.isoformat()[:7])
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    idx = {m: i for i, m in enumerate(months)}
    series = defaultdict(lambda: [0.0] * len(months))
    for r in rows:
        m = r["date"][:7]
        if m in idx:
            series[r[by]][idx[m]] += r["waste"]
    return months, {k: [round(x, 1) for x in v] for k, v in series.items()}


def period_status(db, today: date, months_back=3):
    """Контроль сдачи (ФТ-5.5): по каждой активной организации — статус отчёта за последние месяцы.
    Текущий месяц ещё идёт: черновик там — норма, несданными считаем только прошедшие месяцы."""
    cur = today.replace(day=1)
    months = [cur]
    for _ in range(months_back):
        months.insert(0, (months[0] - timedelta(days=1)).replace(day=1))
    orgs = db.execute("SELECT id, name FROM organizations WHERE active = 1 ORDER BY id").fetchall()
    rep = {(r["org_id"], r["date"]): r["status"] for r in
           db.execute("SELECT org_id, date, status FROM reports WHERE date >= ?", (months[0].isoformat(),))}
    out = []
    for o in orgs:
        cells = []
        for m in months:
            st = rep.get((o["id"], m.isoformat()))
            late = m < cur and st in (None, "draft", "returned")
            cells.append(dict(month=m.isoformat()[:7], status=st or "none", late=late))
        out.append(dict(org=o["name"], org_id=o["id"], cells=cells))
    return dict(months=[m.isoformat()[:7] for m in months], rows=out)


def missing_reports(db, today: date):
    """Кто не сдал отчёт за прошедшие месяцы (черновик и возвращённый — тоже не сдан)."""
    ps = period_status(db, today)
    return [dict(org=r["org"], org_id=r["org_id"], date=c["month"], status=c["status"])
            for r in ps["rows"] for c in r["cells"] if c["late"]]


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
        if r["org_type"] in ("dept_lab", "sci_lab"):
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

    # 5. Сменить способ утилизации на рекомендуемый (справочник CO2): больше всего CO2 по виду отхода
    pot = defaultdict(lambda: [0.0, 0.0, None])   # кг/мес на полигон, кг CO2/мес, рекомендуемый способ
    for r in rows:
        if r["co2_potential"] > 0:
            p = pot[r["wt"]]
            p[0] += r["waste"] / 2; p[1] += r["co2_potential"] / 2; p[2] = r["rec_method"]
    for wt, (kg, co2, method) in sorted(pot.items(), key=lambda kv: -kv[1][1])[:3]:
        if co2 < 20:
            continue
        recs.append(dict(
            kind="Переработка",
            title=tr("{wt}: «{m}» вместо ТБО", wt=tr(wt), m=tr(method)),
            text=tr("{kg} кг/мес сейчас уходит на полигон. Смена способа сократит выбросы на {co2} кг CO₂-экв в месяц "
                    "(DEFRA 2024) и снизит вывоз ТБО.", kg=fmt_num(kg, 0), co2=fmt_num(co2, 0)),
            impact=kg * DISPOSAL_COST_PER_KG, co2=co2))

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
                  "co2_уже_сэкономлено_кг": round(t["co2_avoided"]),
                  "co2_можно_сэкономить_рекомендуемыми_способами_кг": round(t["co2_potential"]),
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
        "фактически_переработано_проц": round(t["recycled_share"]),
        "не_сдали_отчёт": [f"{m['org']} ({m['date']})" for m in missing_reports(db, today)],
        "допущения": ASSUMPTIONS,
    }
