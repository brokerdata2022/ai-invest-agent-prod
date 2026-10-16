"""
Спільний бойлерплейт reporting-скриптів: доступ до БД (sys.path), читання
Telegram-креденшелів, словники емодзі напрямку, читання рядків як dict.

Жодного форматування конкретного звіту тут — воно лишається в кожному
скрипті (reporting/CLAUDE.md: тільки представлення). Винесено те, що
було скопійоване в 6-7 файлах слово в слово:

- `sys.path`-вставка для `common.db` (data-ingestion — не валідне ім'я
  Python-пакета через дефіс, тому додаємо вміст теки напряму). Робиться
  при ІМПОРТІ цього модуля, тож у скрипті рядок `from _common import ...`
  має стояти ПЕРЕД `from common.db import ...`.
- `resolve_telegram_credentials()` — той самий early-exit "токен/чат не
  задані" з main() кожного notify-скрипта.
- `DIRECTION_EMOJI` / `MARKET_DIRECTION_LABEL` — були в 3 і 2 файлах
  відповідно.
- `fetch_dicts()` — `cur.description`-в-dict, був продубльований у
  кожному fetch_*() (raw cursor замість RealDictCursor — щоб не тягнути
  psycopg2.extras у кожен скрипт).
- `mark_notified()` — спільний дедуп-паттерн "позначити рядки як вже
  надіслані" (2026-09-28, критичний фікс — живий фідбек користувача:
  `news_notify.py` слав ІДЕНТИЧНІ ранкове й вечірнє повідомлення, коли
  за день з'являлось мало нового). Той самий принцип, що вже був у
  `expectations_notify.py`/`expectations._db.mark_notified` — тут
  узагальнено на решту 4 notify-скриптів, СВОЇМ кодом (не імпортом з
  analysis/, contained independence нижче).
- `escape_html()`/`bold()`/`link()` — хелпери для Telegram
  `parse_mode="HTML"` (2026-10-03, рішення користувача: гарне
  форматування усіх повідомлень — заголовки/списки/посилання/емодзі).
  `escape_html()` ОБОВ'ЯЗКОВИЙ навколо будь-якого динамічного тексту
  (LLM summary/reasoning, назви компаній, заголовки новин) перед
  вставкою в HTML-рядок — інакше символ "<"/"&" у тексті ламає парсинг
  Telegram (400 Bad Request). Сам Telegram HTML підтримує лише вузький
  набір тегів (b/i/u/s/a/code/pre/blockquote) — немає <ul>/<li>/<h1>,
  тому "заголовки" тут — жирний рядок, а "списки" — рядки з емодзі-
  маркером (той самий принцип, що вже був у форматі повідомлень).
"""

import hashlib
import html
import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data-ingestion")
)

logger = logging.getLogger(__name__)

# Пояс і формат відображення — з ЄДИНОГО config.py у корені (доступний
# через PYTHONPATH=/app, Dockerfile).
from config import DISPLAY_DATETIME_FORMAT, DISPLAY_TIMEZONE  # noqa: E402

# Напрямок сигналу з analysis/ → емодзі. Той самий набір значень, що
# analysis/llm_common.py:DIRECTIONS (без прямого імпорту — reporting/ і
# analysis/ лишаються незалежними деплойментами, reporting/CLAUDE.md).
DIRECTION_EMOJI = {"up": "🟢", "down": "🔴", "neutral": "⚪", "unclear": "❓"}

# Той самий напрямок, але для синтезу стану РИНКУ (market_synthesis):
# up/down тут означають risk-on/risk-off, тому окремий підпис.
MARKET_DIRECTION_LABEL = {
    "up": "🟢 risk-on",
    "down": "🔴 risk-off",
    "neutral": "⚪ збалансовано",
    "unclear": "❓ немає чіткого сигналу",
}


def resolve_telegram_credentials() -> tuple[str, str]:
    """(token, chat_id) з .env або вихід із кодом 1 і зрозумілим
    повідомленням — сповіщення без креденшелів безсенсове, це не та
    помилка, яку варто ковтати."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не задані в .env (див. .env.example)"
        )
        sys.exit(1)
    return token, chat_id


def send_report(text: str, parse_mode: str | None = "HTML") -> dict:
    """Надіслати готовий текст у Telegram — токен і чат беруться з .env.

    **Навіщо хелпер.** `telegram_client.send_telegram_message` має
    порядок `(token, chat_id, text)`, і це легко переставити: живий баг
    2026-10-15 — три щойно написані скрипти (`outcomes_notify`,
    `output_health`, `crosscheck_quotes`) викликали його як
    `(text, token=..., chat_id=...)`. Тести проходили, бо шлях
    відправки виконується ЛИШЕ при справжній події: тижневий звіт
    упав би в понеділок, алерт здоров'я — при першій зміні стану,
    звірка цін — при першій розбіжності. Класичний тихий дефект цього
    проєкту: код виглядає робочим і чекає місяць, щоб упасти.

    Тепер викликач передає лише ТЕКСТ, і переставити нема чого."""
    from telegram_client import send_telegram_message  # noqa: E402

    token, chat_id = resolve_telegram_credentials()
    # Повертаємо результат, щоб викличник не писав "надіслано", коли
    # транспорт відправку ПРОПУСТИВ (AGENT_ENV=dev) — суперечливі два
    # рядки в лозі коштують дорожче за один зайвий return.
    return send_telegram_message(token, chat_id, text, parse_mode=parse_mode)


def fetch_dicts(conn, query: str, params: tuple = ()) -> list[dict]:
    """Рядки запиту як list[dict] (ключі — імена колонок)."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def fetch_one_dict(conn, query: str, params: tuple = ()):
    """Перший рядок як dict, або None якщо запит нічого не повернув."""
    rows = fetch_dicts(conn, query, params)
    return rows[0] if rows else None


# Білий список таблиць для mark_notified() — psycopg2 не вміє
# параметризувати SQL-ідентифікатори (лише значення), тому ім'я
# таблиці підставляється в SQL напряму; безпечно, бо `table` завжди
# літерал у коді виклику (ніколи не користувацький ввід), і білий
# список унеможливлює будь-яке інше ім'я.
_NOTIFIABLE_TABLES = frozenset({
    "news_analysis", "news_synthesis", "market_synthesis", "candidate_assets",
    "news_consolidated", "screening_results", "crypto_screening_candidates",
    "crypto_long_candidates", "calendar_outlook", "metric_forecasts",
    "trading_list", "fundamental_analysis",
    # Додані 2026-10-13: синтез рішення по ставці (останній
    # незакритий пункт плану) і тези з тексту звітів 10-K/10-Q.
    "policy_rate_synthesis", "filing_insights",
})


def escape_html(value) -> str:
    """Екранує '&'/'<'/'>' для Telegram parse_mode="HTML" — обов'язково
    навколо будь-якого динамічного тексту (не навколо тегів, які
    будуємо самі). `quote=False` — лапки в тексті ("), не в атрибутах
    тегів, екранувати не треба (єдине місце з атрибутом — href у
    link() нижче, де escape_html() застосовується саме до URL)."""
    return html.escape(str(value), quote=False)


def bold(value) -> str:
    """'<b>текст</b>' із заголовком (ескейпнутим) усередині."""
    return f"<b>{escape_html(value)}</b>"


def link(text, url) -> str:
    """Клікабельне посилання '<a href="...">текст</a>' — і текст, і URL
    ескейпнуті (href теж може містити "&" у query-рядку)."""
    return f'<a href="{escape_html(url)}">{escape_html(text)}</a>'


def mark_notified(conn, table: str, ids: list[int]) -> None:
    """UPDATE <table> SET notified_at = now() WHERE id = ANY(ids).

    Викликати одразу після успішної відправки в Telegram — щоб
    наступний прогін (news_notify.py — двічі на добу) не надіслав ті
    самі рядки повторно, якщо за вікно з'явилось мало нового."""
    if not ids:
        return
    if table not in _NOTIFIABLE_TABLES:
        raise ValueError(f"mark_notified: непідтримувана таблиця {table!r}")
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {table} SET notified_at = now() WHERE id = ANY(%s)", (ids,))  # noqa: S608 — table з білого списку вище, не користувацький ввід
    conn.commit()


def format_local_dt(value, fmt: str = DISPLAY_DATETIME_FORMAT) -> str:
    """Час у поясі КОРИСТУВАЧА (`config.DISPLAY_TIMEZONE`).

    Живий фідбек користувача 2026-10-05: у Telegram ішло
    "2026-10-08 12:30:00+00:00" — `release_log.scheduled_at` це
    TIMESTAMPTZ, і psycopg2 віддає його в UTC, а f-string друкував як є.

    Значення без часового поясу (naive) трактується як UTC — саме так
    його й віддає БД, якщо сесія без tz; видавати його за локальний
    означало б зсунути час на кілька годин ТИХО.

    Не-дата (рядок, None) віддається як є: notify-скрипт не повинен
    падати через несподіваний тип у полі, яке лише показується."""
    if not isinstance(value, datetime):
        return str(value) if value is not None else ""

    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo(DISPLAY_TIMEZONE)).strftime(fmt)


def composition_changed(conn, notifier: str, item_ids: list) -> bool:
    """True, якщо СКЛАД того, що збираємось надіслати, відрізняється
    від останнього надісланого цим же notify-скриптом.

    **Навіщо (2026-10-13).** Наявний `mark_notified()` дедупить РЯДКИ,
    а скринінг і пошук новачків щопрогону створюють НОВІ рядки з новим
    `run_at` — технічно ненадіслані, змістово ті самі. Живий вивід 12
    жовтня: `notify_screening` надіслав ідентичні 14 тикерів 13-й день
    підряд; `notify_candidates` — ті самі 6 акцій тричі за добу з
    однаковими числами (ціни акцій оновлюються раз на добу о 03:00);
    `notify_crypto_long` — того самого кандидата 4 рази. З ~25
    повідомлень за добу зі справді новим змістом було 6.

    Склад порівнюється ВІДСОРТОВАНИМ: зміна лише порядку при тому
    самому наборі — не новина для читача (а порядок у скринінгу
    "плаває" від дрібних змін ціни).

    Викликати ПЕРЕД надсиланням; `remember_composition()` — після
    успішного. Два кроки, а не один, щоб збій надсилання не
    "з'їв" повідомлення: відпечаток оновлюється лише після того, як
    повідомлення справді пішло.

    Подієві сповіщення (`notify_expectations`, `notify_release_impact`,
    `notify_forecasts`) цього НЕ використовують: там кожна подія
    унікальна й мусить дійти завжди."""
    fingerprint = _fingerprint(item_ids)
    row = fetch_one_dict(
        conn,
        "SELECT fingerprint FROM notify_fingerprints WHERE notifier = %s",
        (notifier,),
    )
    if row is None:
        return True
    return row["fingerprint"] != fingerprint


def remember_composition(conn, notifier: str, item_ids: list) -> None:
    """Запам'ятати склад як надісланий (після успішного надсилання)."""
    fingerprint = _fingerprint(item_ids)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO notify_fingerprints (notifier, fingerprint, item_count, sent_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (notifier) DO UPDATE
               SET fingerprint = EXCLUDED.fingerprint,
                   item_count  = EXCLUDED.item_count,
                   sent_at     = EXCLUDED.sent_at
            """,
            (notifier, fingerprint, len(item_ids)),
        )
    conn.commit()


def _fingerprint(item_ids: list) -> str:
    joined = "|".join(sorted(str(i) for i in item_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
