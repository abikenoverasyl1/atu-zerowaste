"""Русский / казахский интерфейс.
Исходный текст в шаблонах — русский: {{ _('Сегодня') }}. Казахский перевод лежит в словаре KK (i18n_kk.py).
Нет перевода — показывается русский, поэтому сайт никогда не «ломается» от новой строки.
Названия точек и видов отходов из базы тоже переводятся, если они есть в словаре.
"""
from flask import request, session

from i18n_kk import KK

LANGS = {"ru": "Русский", "kk": "Қазақша"}


def get_lang():
    lang = session.get("lang") or request.cookies.get("lang") or "ru"
    return lang if lang in LANGS else "ru"


def gettext(source, /, **kw):
    if source is None:
        return ""
    text = KK.get(source, source) if get_lang() == "kk" else source
    return text.format(**kw) if kw else text


# Строки, которые пишет JavaScript: tr('...') в шаблонах — собираем при старте
import os as _os
import re as _re

_TPL = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "templates")
JS_KEYS = set()
for _f in _os.listdir(_TPL):
    if _f.endswith(".html"):
        JS_KEYS.update(_re.findall(r"tr\('([^']+)'", open(_os.path.join(_TPL, _f), encoding="utf-8").read()))


def js_translations():
    return {k: KK[k] for k in JS_KEYS if k in KK} if get_lang() == "kk" else {}
