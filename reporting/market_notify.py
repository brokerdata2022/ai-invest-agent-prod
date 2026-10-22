#!/usr/bin/env python3
"""
Сповіщення в Telegram про стан ринку (risk-on/risk-off) — вже
пораховане analysis/news_analysis/synthesize_market.py (market_synthesis).
Тільки форматування готового результату, жодної аналітики
(reporting/CLAUDE.md).

Дедуп (2026-09-28, критичний фікс, той самий принцип що
news_notify.py): `notified_at IS NULL` — без цього повторний запуск
джоби (напр. ретрай runner.py при транзієнтному збої відправки) слав
би той самий висновок дня вдруге.

**Сесійний бриф (2026-10-22, пункт 5 плану сесії).** Повідомлення
сесії тепер містить і АКТИВИ ДЛЯ ТОРГІВ у цю сесію (останній прогін
`trading_list`, класи — `config.SESSION_ASSET_CLASSES`) з причинами й
рівнями; якщо таких немає — прямо так і сказано. Надсилається одразу
після синтезу (`orchestration/session_brief.py`), а не окремою джобою
через годину.

Використання (після analysis/news_analysis/synthesize_market.py):
    python market_notify.py
    python market_notify.py --dry-run     # надрукувати, не надсилати
"""

import argparse
import json
import logging
from decimal import Decimal

from dotenv import load_dotenv

import config

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import (  # noqa: E402
    MARKET_DIRECTION_LABEL,
    bold,
    escape_html,
    fetch_dicts,
    fetch_one_dict,
    mark_notified,
    resolve_telegram_credentials,
)
from common.db import get_connection  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def fetch_unnotified_market_synthesis(conn, session: str | None = None):
    """Останній ненадісланий синтез. З `session` (сесійний бриф,
    2026-10-22) — лише СВІЖИЙ синтез САМЕ цієї сесії: живий прогін
    показав, що без цього бриф Нью-Йорка, для якого синтезу не
    склалось, підхопив би старий ненадісланий синтез Лондона."""
    if session is None:
        query = """
            SELECT id, cluster_count, direction, confidence, summary, source_refs,
                   created_at, session
            FROM market_synthesis
            WHERE notified_at IS NULL
            ORDER BY created_at DESC
            LIMIT 1
        """
        return fetch_one_dict(conn, query)
    query = """
        SELECT id, cluster_count, direction, confidence, summary, source_refs,
               created_at, session
        FROM market_synthesis
        WHERE notified_at IS NULL
          AND session = %s
          AND created_at >= now() - make_interval(hours => %s)
        ORDER BY created_at DESC
        LIMIT 1
    """
    return fetch_one_dict(conn, query, (session, config.SESSION_BRIEF_MAX_AGE_HOURS))


# Сесія → заголовок повідомлення. 'daily' — старі рядки до переходу
# на сесійний розклад (db/schema.sql, 2026-10-04).
SESSION_TITLE = {
    "asia": "🌏 Перед відкриттям Токіо",
    "europe": "🌍 Перед відкриттям Лондона",
    "us": "🌎 Перед відкриттям Нью-Йорка",
    "daily": "🌍 Стан ринку",
}


def fetch_session_trading(conn, session: str):
    """Активи останнього прогону списку для торгів, що торгуються в цю
    сесію. None — для сесії немає переліку класів (стара 'daily')."""
    kinds = config.SESSION_ASSET_CLASSES.get(session)
    if kinds is None:
        return None
    return fetch_dicts(
        conn,
        """
        SELECT id, asset_id, label, kind, direction, why, levels
        FROM trading_list
        WHERE run_at = (SELECT MAX(run_at) FROM trading_list_runs)
          AND kind = ANY(%s)
        ORDER BY score DESC
        """,
        (list(kinds),),
    )


_DIRECTION_ARROW = {"up": "🟢 вгору", "down": "🔴 вниз"}


def _price(value) -> str:
    """107.56000000 → 107.56; 60.119998931884766 → 60.12; 0.000123 → 0.000123."""
    try:
        number = Decimal(str(value))
    except Exception:
        return str(value)
    return f"{number:,.2f}" if abs(number) >= 1 else f"{number:.4g}"


def _levels_text(levels) -> str:
    if not levels:
        return ""
    if isinstance(levels, str):
        levels = json.loads(levels)
    if "high" not in levels:
        return f"ціна {_price(levels.get('last'))}"
    daily = f", звичайний денний рух ±{levels['daily_move_pct']}%" if levels.get("daily_move_pct") else ""
    return (
        f"ціна {_price(levels['last'])}; макс {levels['days']} дн {_price(levels['high'])} ({levels['to_high_pct']}%), "
        f"мін {_price(levels['low'])} ({levels['to_low_pct']}%){daily}"
    )


def format_trading_block(items) -> list[str]:
    """Блок "чим торгувати в цю сесію" — або ПРЯМО "нічим"."""
    if items is None:
        return []
    if not items:
        return ["", bold("🎯 Активи для торгів у цю сесію: немає"),
                "<i>Жоден актив не має власної події сьогодні з однозначним напрямком.</i>"]
    lines = ["", bold(f"🎯 Активи для торгів у цю сесію — {len(items)}")]
    for item in items:
        name = escape_html(item.get("label") or item["asset_id"])
        arrow = _DIRECTION_ARROW.get(item["direction"], escape_html(item["direction"]))
        lines.append(f"• {bold(name)} — {arrow}")
        if item.get("why"):
            lines.append(f"   {escape_html(str(item['why'])[:220])}")
        levels = _levels_text(item.get("levels"))
        if levels:
            lines.append(f"   📏 {escape_html(levels)}")
    lines.append("<i>ℹ️ Опис стану й подій, не вказівка діяти.</i>")
    return lines


def format_brief_without_synthesis(session: str, trading) -> str:
    """Бриф, коли синтез сесії не склався (немає підтверджених історій
    за вікно): активи для торгів однаково потрібні перед відкриттям."""
    title = SESSION_TITLE.get(session, "🌍 Стан ринку")
    lines = [bold(title), "<i>Підтверджених новинних історій за вікно немає — синтезу стану ринку цього разу немає.</i>"]
    lines.extend(format_trading_block(trading))
    return "\n".join(lines)


def format_message(row, trading=None) -> str:
    if row is None:
        return "🌍 Синтезу стану ринку ще немає."

    label = MARKET_DIRECTION_LABEL.get(row["direction"], row["direction"])
    confidence = float(row["confidence"])
    # Сесія в заголовку (2026-10-04): з трьома синтезами на добу без неї
    # неможливо зрозуміти, про який момент доби йдеться — а саме момент
    # і визначає, як читати ті самі новини (analysis/news_analysis/
    # synthesize_market.py:SESSIONS).
    session_title = SESSION_TITLE.get(row.get("session") or "daily", "🌍 Стан ринку")
    lines = [
        bold(f"{session_title}: {label} (упевненість {confidence:.2f})"),
        escape_html(row["summary"]),
        "",
        bold(f"На основі {row['cluster_count']} найбільш підтверджених історій:"),
    ]
    for ref in row["source_refs"]:
        lines.append(f"• [{ref['source_count']} джерел] {escape_html(ref['title'])}")
    lines.extend(format_trading_block(trading))
    return "\n".join(lines).rstrip()


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--session", choices=sorted(config.SESSION_ASSET_CLASSES), default=None,
        help="сесійний бриф: лише свіжий синтез цієї сесії, а без нього — активи для торгів",
    )
    parser.add_argument("--dry-run", action="store_true", help="надрукувати, не надсилати й не позначати")
    args = parser.parse_args()

    token, chat_id = (None, None) if args.dry_run else resolve_telegram_credentials()

    conn = get_connection()
    try:
        row = fetch_unnotified_market_synthesis(conn, args.session)
        if row is None and args.session is None:
            logger.info("Нового синтезу стану ринку немає — сповіщення не надсилається")
            return

        session = args.session or row.get("session") or "daily"
        trading = fetch_session_trading(conn, session)
        text = format_message(row, trading) if row else format_brief_without_synthesis(session, trading)
        if args.dry_run:
            print(text)
            return
        send_telegram_message(token, chat_id, text, parse_mode="HTML")
        if row:
            mark_notified(conn, "market_synthesis", [row["id"]])
        # Активи, що пішли в бриф, — уже надіслані: окремий звіт списку
        # торгів їх не повторить.
        if trading:
            mark_notified(conn, "trading_list", [item["id"] for item in trading])
        logger.info("Надіслано в Telegram: стан ринку %s", row["direction"])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
