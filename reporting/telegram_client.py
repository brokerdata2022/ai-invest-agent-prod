"""
Спільний HTTP-хелпер для надсилання повідомлень у Telegram — виніс із
telegram_notify.py/news_notify.py (мали буквально ідентичну функцію),
щоб orchestration/alerts.py міг перевикористати той самий код замість
третьої копії.

`parse_mode` (2026-10-03, рішення користувача — гарне форматування
усіх повідомлень): необов'язковий, за замовчуванням None (як і було —
plain text, без жодної інтерпретації). `orchestration/alerts.py`
свідомо лишається на дефолті (ops-алерти про провал джоби мають
надійти, навіть якщо текст випадково містить "<"/"&" — з parse_mode
"HTML" такий символ дав би 400 Bad Request і зламав сам алерт);
reporting/-скрипти, що будують HTML (`_common.py:escape_html/bold/
link`), передають `parse_mode="HTML"` явно.

`get_updates()`/`set_my_commands()` (2026-10-03, Telegram-команди
людською мовою — `orchestration/telegram_commands.py`): той самий
принцип — тонка HTTP-обгортка, жодної логіки парсингу/диспетчеризації
команд тут (вона в samому telegram_commands.py)."""

import requests

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"


def send_telegram_message(token: str, chat_id: str, text: str, parse_mode: str | None = None) -> dict:
    url = TELEGRAM_API_BASE.format(token=token, method="sendMessage")
    data = {"chat_id": chat_id, "text": text}
    if parse_mode:
        data["parse_mode"] = parse_mode
    response = requests.post(url, data=data, timeout=15)
    response.raise_for_status()
    return response.json()


def get_updates(token: str, offset: int | None = None, timeout: int = 0) -> list[dict]:
    """Короткий polling Telegram `getUpdates` — повертає список Update-
    об'єктів (може бути порожнім, якщо нових повідомлень немає).
    `offset` — update_id останнього вже обробленого +1 (Telegram сам
    більше не повертає підтверджені оновлення після цього)."""
    url = TELEGRAM_API_BASE.format(token=token, method="getUpdates")
    params: dict = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    response = requests.get(url, params=params, timeout=timeout + 10)
    response.raise_for_status()
    return response.json().get("result", [])


def set_my_commands(token: str, commands: list[dict]) -> dict:
    """`commands` — [{'command': 'job_name', 'description': '...'}].
    Реєструє "/"-меню бота в Telegram UI (назва+опис видно одразу при
    наборі "/"). Не щоразу з опитуванням — окремий ручний скрипт
    (`orchestration/register_telegram_commands.py`), бо список команд
    змінюється рідко (новий job)."""
    url = TELEGRAM_API_BASE.format(token=token, method="setMyCommands")
    response = requests.post(url, json={"commands": commands}, timeout=15)
    response.raise_for_status()
    return response.json()
