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

import logging
import os

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"


# Середовища, з яких НЕ можна писати живому користувачу. Перевіряється
# в єдиній точці відправки — тут, а не в кожному notify-скрипті
# (їх 30, і забути один означало б лишити дірку).
NON_PRODUCTION_ENVS = frozenset({"dev", "development", "test", "local", "staging"})


def is_sending_blocked() -> str | None:
    """Назва середовища, якщо відправка заборонена, інакше None.

    **Навіщо (живий випадок 2026-10-16).** Дев-стек і продакшен
    використовують ОДИН токен бота. Користувач отримав у Telegram
    відповідь, зібрану з дев-бази — відновленого знімка від 13.10 — і
    вона була зовні невідрізнима від продакшенівської: ті самі
    формулювання, той самий бот. Розбір показав, що запис ішов із
    дев-контейнера, і це означає, що будь-який ручний прогін у деві
    може надіслати користувачу застарілу картину ринку.

    **Чому «блокувати лише явно позначене НЕ-продакшен», а не
    «дозволяти лише явно позначений продакшен».** Друге виглядає
    строгішим, але якби змінної не було в продакшен-`.env`, воно
    вимкнуло б УСІ сповіщення агента — тихо й одразу. Асиметрія
    навмисна: помилка в цей бік коштує «дев щось надіслав», у той —
    «агент замовк і ти про це не дізнався».
    """
    env = (os.environ.get("AGENT_ENV") or "").strip().lower()
    return env if env in NON_PRODUCTION_ENVS else None


def send_telegram_message(token: str, chat_id: str, text: str, parse_mode: str | None = None) -> dict:
    blocked_env = is_sending_blocked()
    if blocked_env:
        # Не виняток: прогін у деві має доходити до кінця (нам потрібен
        # результат), просто без доставки живому користувачу.
        logger.warning(
            "AGENT_ENV=%s — відправка в Telegram ПРОПУЩЕНА (%d символів). "
            "Так зроблено, щоб дев-прогін не надсилав користувачу дані "
            "дев-бази; у продакшені AGENT_ENV не задається.",
            blocked_env, len(text),
        )
        return {"ok": False, "skipped_env": blocked_env}

    url = TELEGRAM_API_BASE.format(token=token, method="sendMessage")
    result: dict = {}
    for chunk in split_message(text):
        data = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            data["parse_mode"] = parse_mode
        response = requests.post(url, data=data, timeout=15)
        _raise_without_token(response)
        result = response.json()
    return result


# Ліміт Telegram — 4096 символів на повідомлення; запас на підрахунок
# після розбору HTML-сутностей.
TELEGRAM_MAX_CHARS = 4000


class TelegramError(RuntimeError):
    """Відмова Telegram API — з поясненням від Telegram і БЕЗ токена."""


def _raise_without_token(response) -> None:
    """Замість `raise_for_status()` (2026-10-22, живий випадок на
    продакшені): той клав у текст помилки URL запиту, а в URL — ТОКЕН
    бота, і трасування з токеном ішли в логи й в алерти. До того ж
    "400 Bad Request" не казав, ЩО не так; Telegram пояснює це в тілі
    (`description`: "message is too long", "can't parse entities")."""
    status = getattr(response, "status_code", 200)
    if status < 400:
        return
    try:
        description = response.json().get("description", "")
    except ValueError:
        description = (getattr(response, "text", "") or "")[:200]
    raise TelegramError(f"Telegram API {status}: {description}")


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Довге повідомлення → частини до `limit`, по межах РЯДКІВ.

    Навіщо (2026-10-22, живий збій notify_fundamental на продакшені):
    розбір активу за чинниками з блоком компанії перевищив 4096
    символів, Telegram відповів 400, і через одне повідомлення не
    надсилалось жодне наступне — щодня знову. Ділимо по рядках, бо
    HTML-теги наших звітів не переносяться через рядок; рядок, довший
    за ліміт (суцільний абзац), ділиться по пробілу."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:cut])
            line = line[cut:].lstrip()
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


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
    _raise_without_token(response)
    return response.json().get("result", [])


def set_my_commands(token: str, commands: list[dict]) -> dict:
    """`commands` — [{'command': 'job_name', 'description': '...'}].
    Реєструє "/"-меню бота в Telegram UI (назва+опис видно одразу при
    наборі "/"). Не щоразу з опитуванням — окремий ручний скрипт
    (`orchestration/register_telegram_commands.py`), бо список команд
    змінюється рідко (новий job)."""
    url = TELEGRAM_API_BASE.format(token=token, method="setMyCommands")
    response = requests.post(url, json={"commands": commands}, timeout=15)
    _raise_without_token(response)
    return response.json()
