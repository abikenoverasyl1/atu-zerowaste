"""Уведомления в Telegram. Работает, если заданы переменные окружения:
TELEGRAM_BOT_TOKEN  — токен бота от @BotFather
TELEGRAM_CHAT_ID    — id канала/группы для объявлений об излишках (например, @atu_izlishki)
Без них сообщения просто пишутся в лог — приложение не ломается.
"""
import logging
import os

import requests

log = logging.getLogger("notify")


def send_telegram(text: str, chat_id: str | None = None) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log.info("[telegram выключен] %s", text)
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text}, timeout=5)
        return r.ok
    except requests.RequestException as e:
        log.warning("Telegram недоступен: %s", e)
        return False
