"""Выгрузка реестра и сводного отчёта в Excel и PDF."""
import io
import os
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

import analytics as an

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "fonts")
# Где искать шрифт с кириллицей для PDF: в проекте, потом в системе. Нет нигде — PDF без кириллицы, но сайт работает.
_FONT_CANDIDATES = [
    (os.path.join(FONT_DIR, "DejaVuSans.ttf"), os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
]
_fonts = None


def pdf_fonts():
    """Регистрирует шрифты при первом PDF, а не при запуске — отсутствие шрифта не роняет сайт."""
    global _fonts
    if _fonts:
        return _fonts
    for regular, bold in _FONT_CANDIDATES:
        if os.path.exists(regular):
            try:
                pdfmetrics.registerFont(TTFont("DejaVu", regular))
                pdfmetrics.registerFont(TTFont("DejaVu-Bold", bold if os.path.exists(bold) else regular))
                _fonts = ("DejaVu", "DejaVu-Bold")
                return _fonts
            except Exception:  # noqa: BLE001 — битый файл шрифта: пробуем следующий
                continue
    _fonts = ("Helvetica", "Helvetica-Bold")
    return _fonts

HEADER_FILL = PatternFill("solid", fgColor="1F6B4A")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def _style_header(ws, row=1):
    for cell in ws[row]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)


def _autowidth(ws, max_width=45):
    widths = defaultdict(int)
    for row in ws.iter_rows():
        for c in row:
            if c.value is not None:
                widths[c.column] = max(widths[c.column], len(str(c.value)))
    for col, w in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = min(max(w + 2, 8), max_width)


def registry_xlsx(rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Реестр"
    ws.append(["Дата", "Организация", "Вид отхода", "Ед.", "Всего", "Отдано (излишек)", "В отходы",
               "Потери, ₸", "CO₂, кг", "Перерабатываемый"])
    _style_header(ws)
    for r in rows:
        ws.append([r["date"], r["org"], r["wt"], r["unit"], r["qty"], r["surplus_qty"], round(r["waste"], 1),
                   round(r["money"]), round(r["co2"], 1), "да" if r["recyclable"] else "нет"])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    _autowidth(ws)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def summary_data(rows):
    orgs = sorted({r["org"] for r in rows})
    types = sorted({(r["wt"], r["unit"]) for r in rows})
    pivot = defaultdict(float)
    for r in rows:
        pivot[(r["org"], r["wt"])] += r["waste"]
    by_org = defaultdict(lambda: dict(waste=0.0, money=0.0, co2=0.0, surplus=0.0))
    for r in rows:
        o = by_org[r["org"]]
        o["waste"] += r["waste"]; o["money"] += r["money"]; o["co2"] += r["co2"]; o["surplus"] += r["surplus_qty"]
    return orgs, types, pivot, by_org


def summary_xlsx(rows, title, period_label):
    orgs, types, pivot, by_org = summary_data(rows)
    t = an.totals(rows)
    wb = Workbook()

    ws = wb.active
    ws.title = "Итоги"
    ws.append([title])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"Период: {period_label}"])
    ws.append([])
    ws.append(["Организация", "Отходы, кг/л", "Потери, ₸", "CO₂, кг", "Отдано излишков, кг"])
    _style_header(ws, 4)
    for org in sorted(by_org, key=lambda k: -by_org[k]["money"]):
        o = by_org[org]
        ws.append([org, round(o["waste"], 1), round(o["money"]), round(o["co2"], 1), round(o["surplus"], 1)])
    ws.append(["ИТОГО", round(t["waste"], 1), round(t["money"]), round(t["co2"], 1), round(t["surplus"], 1)])
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
    ws.append([])
    ws.append([f"Доля пригодных к переработке: {t['recyclable_share']:.0f}%"])
    ws.append([f"Сэкономлено за счёт раздачи излишков: {an.fmt_money(t['saved_money'])}, "
               f"{t['saved_co2']:.0f} кг CO₂"])
    ws.append([])
    ws.append(["Допущения расчёта:"])
    for a in an.ASSUMPTIONS:
        ws.append(["• " + a])
    _autowidth(ws, 60)

    ws2 = wb.create_sheet("По видам отходов")
    ws2.append(["Организация"] + [f"{w} ({u})" for w, u in types] + ["Итого"])
    _style_header(ws2)
    for org in orgs:
        vals = [round(pivot.get((org, w), 0), 1) for w, _ in types]
        ws2.append([org] + vals + [round(sum(vals), 1)])
    ws2.freeze_panes = "B2"
    _autowidth(ws2, 22)

    ws3 = wb.create_sheet("Реестр")
    ws3.append(["Дата", "Организация", "Вид отхода", "Ед.", "В отходы", "Потери, ₸", "CO₂, кг"])
    _style_header(ws3)
    for r in rows:
        ws3.append([r["date"], r["org"], r["wt"], r["unit"], round(r["waste"], 1), round(r["money"]), round(r["co2"], 1)])
    _autowidth(ws3)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def summary_pdf(rows, title, period_label):
    orgs, types, pivot, by_org = summary_data(rows)
    t = an.totals(rows)
    F, FB = pdf_fonts()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm, title=title)
    h1 = ParagraphStyle("h1", fontName=FB, fontSize=15, spaceAfter=4)
    p = ParagraphStyle("p", fontName=F, fontSize=9, leading=12)
    small = ParagraphStyle("s", fontName=F, fontSize=7.5, leading=10, textColor=colors.grey)

    story = [Paragraph(title, h1), Paragraph(f"Период: {period_label}", p), Spacer(1, 6)]
    kpi = (f"<b>Отходы:</b> {an.fmt_num(t['waste'], 0)} кг/л &nbsp;&nbsp; <b>Потери:</b> {an.fmt_money(t['money'])}"
           f" &nbsp;&nbsp; <b>CO₂:</b> {an.fmt_num(t['co2'], 0)} кг &nbsp;&nbsp; <b>Отдано излишков:</b> "
           f"{an.fmt_num(t['surplus'], 0)} кг (сэкономлено {an.fmt_money(t['saved_money'])})")
    story += [Paragraph(kpi, ParagraphStyle("k", parent=p, fontName=F)), Spacer(1, 8)]

    data = [["Организация", "Отходы, кг/л", "Потери, ₸", "CO₂, кг", "Излишки, кг"]]
    for org in sorted(by_org, key=lambda k: -by_org[k]["money"]):
        o = by_org[org]
        data.append([org, an.fmt_num(o["waste"]), an.fmt_money(o["money"]), an.fmt_num(o["co2"]),
                     an.fmt_num(o["surplus"])])
    data.append(["ИТОГО", an.fmt_num(t["waste"]), an.fmt_money(t["money"]), an.fmt_num(t["co2"]),
                 an.fmt_num(t["surplus"])])
    tbl = Table(data, colWidths=[110 * mm, 35 * mm, 40 * mm, 30 * mm, 30 * mm], repeatRows=1)
    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), F),
        ("FONTNAME", (0, 0), (-1, 0), FB),
        ("FONTNAME", (0, -1), (-1, -1), FB),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F6B4A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#F1F6F3")]),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [tbl, Spacer(1, 10)]

    # Структура по видам отходов (топ-12)
    by_type = an.group_sum(rows, "wt")
    data2 = [["Вид отхода", "Масса", "Доля"]]
    total = sum(by_type.values()) or 1
    for wt, v in list(by_type.items())[:12]:
        data2.append([wt, an.fmt_num(v), f"{v / total * 100:.1f}%"])
    tbl2 = Table(data2, colWidths=[110 * mm, 35 * mm, 25 * mm])
    tbl2.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), F),
        ("FONTNAME", (0, 0), (-1, 0), FB),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E3EEE8")),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
    ]))
    story += [Paragraph("<b>Структура отходов</b>", p), Spacer(1, 3), tbl2, Spacer(1, 10)]
    story += [Paragraph("Допущения: " + "; ".join(an.ASSUMPTIONS), small)]
    doc.build(story)
    return buf.getvalue()
