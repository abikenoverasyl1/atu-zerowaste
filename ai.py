"""ИИ-помощник. Поддерживает три провайдера — какой ключ задан, тот и используется:

  GEMINI_API_KEY     — Google Gemini (БЕСПЛАТНЫЙ уровень в Google AI Studio, есть фото). Модель: GEMINI_MODEL
  GROQ_API_KEY       — Groq (бесплатный уровень, только текст: сводка и вопросы). Модель: GROQ_MODEL
  ANTHROPIC_API_KEY  — Claude (платный, есть фото). Модель: AI_MODEL

Порядок, если заданы несколько: AI_PROVIDER=gemini|groq|claude, иначе Gemini → Groq → Claude.
Без ключей всё продолжает работать: сводка строится по шаблону, фото и вопросы просто недоступны.
"""
import base64
import json
import logging
import os
import re

import requests

log = logging.getLogger("ai")
last_error = ""   # последняя ошибка провайдера — показываем пользователю, чтобы было понятно, что чинить

SYSTEM = (
    "Ты — аналитик системы учёта пищевых отходов Алматинского технологического университета (АТУ). "
    "Организации: столовый комплекс (ИП «Искандер»), учебно-гостиничный комплекс ATU INN, кофейня (ИП «Canella»), "
    "кафедры ТПП и ТХиПП (учебные лаборатории). Отчёт сдаётся за месяц, каждая запись — взвешивание со способом утилизации. "
    "Отвечай по-русски, коротко и конкретно, простыми словами — тебя читают повара, лаборанты и руководство. "
    "Опирайся только на переданные данные, называй цифры с единицами (кг, ₸, %). "
    "Если данных не хватает — так и скажи. Стоимость в тенге — оценка по справочнику, не бухгалтерия. "
    "Без markdown-заголовков и таблиц, максимум 6 коротких пунктов или абзацев."
)

LANG_NOTE = {"kk": "\n\nОтветь на казахском языке (қазақ тілінде)."}
KEYS = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "claude": "ANTHROPIC_API_KEY"}
SEES_IMAGES = {"gemini": True, "groq": False, "claude": True}
NAMES = {"gemini": "Gemini", "groq": "Groq", "claude": "Claude"}


def provider():
    forced = os.environ.get("AI_PROVIDER", "").lower()
    if forced in KEYS and os.environ.get(KEYS[forced]):
        return forced
    return next((p for p in ("gemini", "groq", "claude") if os.environ.get(KEYS[p])), None)


def enabled() -> bool:
    return provider() is not None


def can_see_images() -> bool:
    p = provider()
    return bool(p and SEES_IMAGES[p])


def name() -> str:
    p = provider()
    return NAMES.get(p, "")


ERR_HINTS = {400: "неверный запрос или ключ", 401: "ключ не подходит", 403: "ключ не подходит или API не включён",
             404: "модель не найдена — проверьте GEMINI_MODEL", 429: "превышен бесплатный лимит, подождите минуту",
             500: "сбой у провайдера", 503: "провайдер перегружен, попробуйте позже"}


def _post(url, headers, body, retry_body=None):
    global last_error
    try:
        r = requests.post(url, headers=headers, json=body, timeout=55)
        if r.status_code == 400 and retry_body is not None:     # модель не поддерживает настройку — повтор без неё
            r = requests.post(url, headers=headers, json=retry_body, timeout=55)
        if not r.ok:
            log.warning("ИИ %s: %s %s", url.split("/")[2], r.status_code, r.text[:300])
            last_error = f"{r.status_code}: {ERR_HINTS.get(r.status_code, 'ошибка провайдера')}"
            return None
        last_error = ""
        return r.json()
    except requests.Timeout:
        last_error = "ИИ не ответил вовремя"
        log.warning("ИИ: таймаут")
        return None
    except requests.RequestException as e:
        last_error = "нет связи с провайдером ИИ"
        log.warning("ИИ недоступен: %s", e)
        return None


def _call(content, max_tokens=700, system=SYSTEM):
    """content — строка или список частей: {"type": "text", "text": ...} / {"type": "image", "media_type", "data"(bytes)}."""
    parts = [{"type": "text", "text": content}] if isinstance(content, str) else content
    p = provider()
    if p is None:
        return None
    key = os.environ[KEYS[p]]

    if p == "gemini":
        model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
        gparts = [{"text": x["text"]} if x["type"] == "text" else
                  {"inline_data": {"mime_type": x["media_type"], "data": base64.b64encode(x["data"]).decode()}}
                  for x in parts]
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": gparts}],
                "generationConfig": {"maxOutputTokens": max_tokens * 3, "temperature": 0.3}}
        # Без «размышлений» ответ приходит в разы быстрее; если модель так не умеет — повтор без настройки
        fast = json.loads(json.dumps(body))
        fast["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
        data = _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                     {"x-goog-api-key": key, "content-type": "application/json"}, fast, retry_body=body)
        try:
            return "".join(pt.get("text", "") for pt in data["candidates"][0]["content"]["parts"]).strip() if data else None
        except (KeyError, IndexError, TypeError):
            log.warning("Gemini: неожиданный ответ %s", str(data)[:300])
            return None

    if p == "groq":
        if any(x["type"] == "image" for x in parts):
            return None
        model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
        data = _post("https://api.groq.com/openai/v1/chat/completions",
                     {"authorization": f"Bearer {key}", "content-type": "application/json"},
                     {"model": model, "max_tokens": max_tokens, "temperature": 0.3,
                      "messages": [{"role": "system", "content": system},
                                   {"role": "user", "content": "\n\n".join(x["text"] for x in parts)}]})
        try:
            return data["choices"][0]["message"]["content"].strip() if data else None
        except (KeyError, IndexError, TypeError):
            return None

    # claude
    model = os.environ.get("AI_MODEL", "claude-haiku-5-5")
    cparts = [{"type": "text", "text": x["text"]} if x["type"] == "text" else
              {"type": "image", "source": {"type": "base64", "media_type": x["media_type"],
                                           "data": base64.b64encode(x["data"]).decode()}} for x in parts]
    data = _post("https://api.anthropic.com/v1/messages",
                 {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                 {"model": model, "max_tokens": max_tokens, "system": system,
                  "messages": [{"role": "user", "content": cparts}]})
    if not data:
        return None
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


# ---------- 1. Распознавание отхода по фото ----------
def classify_photo(image_bytes: bytes, media_type: str, options: list[dict]):
    """options: [{'id': 3, 'name': 'Хлеб и выпечка'}, ...] — виды отходов этой точки.
    Возвращает {'id', 'name', 'confidence', 'contamination', 'note'} или None."""
    names = "\n".join(f"{o['id']}: {o['name']}" for o in options)
    prompt = (
        "На фото — контейнер или поднос с пищевыми отходами на кухне или в лаборатории. "
        f"Выбери ОДИН наиболее подходящий вид отхода из списка:\n{names}\n\n"
        "Проверь, нет ли среди пищевых отходов посторонних предметов (пластик, упаковка, салфетки, стекло) — "
        "они мешают отправить отходы на корм или компост. Вес по фото НЕ оценивай.\n"
        'Ответь только JSON: {"id": <число из списка>, "confidence": "высокая|средняя|низкая", '
        '"contamination": "<что постороннее видно или пустая строка>", "note": "<одна короткая фраза: что на фото>"}'
    )
    content = [{"type": "image", "media_type": media_type, "data": image_bytes}, {"type": "text", "text": prompt}]
    text = _call(content, max_tokens=300, system="Ты распознаёшь пищевые отходы на фото. Отвечай строго JSON.")
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        res = json.loads(m.group(0))
        valid = {o["id"]: o["name"] for o in options}
        wid = int(res.get("id", 0))
        if wid not in valid:
            return None
        return dict(id=wid, name=valid[wid], confidence=res.get("confidence", ""),
                    contamination=(res.get("contamination") or "").strip(), note=(res.get("note") or "").strip())
    except (ValueError, TypeError):
        return None


# ---------- 2. Сводка для руководства и ответы на вопросы ----------
def summary(ctx: dict, lang="ru"):
    prompt = (
        "Вот данные учёта пищевых отходов АТУ (JSON):\n" + json.dumps(ctx, ensure_ascii=False) +
        "\n\nСоставь сводку для проректора на 4–6 пунктов: главный итог в цифрах, где теряем больше всего, "
        "что изменилось за последний месяц, 2–3 конкретных действия с ожидаемой экономией в ₸ в месяц."
        + LANG_NOTE.get(lang, "")
    )
    return _call(prompt, max_tokens=700)


def answer(question: str, ctx: dict, lang="ru"):
    prompt = ("Данные учёта пищевых отходов АТУ (JSON):\n" + json.dumps(ctx, ensure_ascii=False) +
              f"\n\nВопрос сотрудника: {question.strip()[:500]}" + LANG_NOTE.get(lang, ""))
    return _call(prompt, max_tokens=600)


def fallback_summary(ctx: dict, tr=None) -> str:
    """Сводка по шаблону — когда ключа ИИ нет или API недоступен. Демо не ломается."""
    from analytics import fmt_money, fmt_num
    tr = tr or (lambda x, **kw: x.format(**kw) if kw else x)
    t = ctx["итого"]
    lines = [tr("За последние {d} дней в отходы ушло {kg} кг, потери — около {m}.", d=ctx["дней"],
                kg=fmt_num(t['отходы_кг'], 0), m=fmt_money(t['потери_тг']))]
    if ctx["по_точкам"]:
        top = ctx["по_точкам"][0]
        share = round(top["потери_тг"] / t["потери_тг"] * 100) if t["потери_тг"] else 0
        lines.append(tr("Больше всего теряет «{org}»: {m}, это {p}% всех потерь.", org=tr(top['точка']),
                        m=fmt_money(top['потери_тг']), p=share))
    if ctx["по_видам"]:
        tp = ctx["по_видам"][0]
        lines.append(tr("Главный вид отходов — {wt}: {kg} {u}.", wt=tr(tp['вид']).lower(), kg=fmt_num(tp['кг'], 0),
                        u=tr(tp['ед'])))
    m = ctx.get("полные_месяцы") or []
    if len(m) >= 2 and m[-2]["кг"]:
        ch = (m[-1]["кг"] / m[-2]["кг"] - 1) * 100
        lines.append(tr("{a} по сравнению с месяцем «{b}»: {p}% по массе.", a=tr(m[-1]['месяц']).capitalize(),
                        b=tr(m[-2]['месяц']), p=f"{'+' if ch >= 0 else ''}{ch:.0f}"))
    for r in ctx.get("рекомендации", [])[:3]:
        lines.append(tr("{t} — экономия около {m} в месяц.", t=r['заголовок'], m=fmt_money(r['экономия_тг_мес'])))
    if t.get("отдано_студентам_кг"):
        lines.append(tr("Студентам отдали {kg} кг еды вместо мусора.", kg=fmt_num(t['отдано_студентам_кг'])))
    return "\n".join("• " + x for x in lines)
