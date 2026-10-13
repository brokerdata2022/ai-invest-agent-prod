#!/usr/bin/env python3
"""
Щоденний дайджест (docs/decisions.md, "Ціль 5"): кілька СПРАВДІ
важливих подій за останні --hours, не список усього релевантного.
Жодного нового LLM-виклику — усі "важливі події" вже пораховані
попередніми кроками (news_synthesis/market_synthesis/
expectation_comparisons/candidate_assets), тут лише форматування
готового (reporting/CLAUDE.md).

Обмеження Telegram (4096 символів на повідомлення) — рішення
користувача: не різати один довгий текст на частини, кожна подія з
топ-списку йде ОКРЕМИМ повідомленням (природно короткі за
конструкцією, ліміт ніколи не досягається).

Дедуп (2026-09-28, критичний фікс, той самий принцип що
news_notify.py/market_notify.py/synthesis_notify.py/candidates_notify.py):
`notified_at IS NULL` на всіх 4 джерелах + `mark_notified()` після
відправки. Раніше вікно `--hours` бралось СЛІПО за `created_at`, тому
цей дайджест дублював один в один усе, що окремі notify_*-джоби вже
надіслали протягом дня (напр. notify_synthesis@6:45,
notify_market_synthesis@18:45) — живий фідбек користувача 2026-09-28,
той самий рядок news_synthesis/market_synthesis приходив у Telegram
двічі символ-в-символ.

Використання:
    python daily_digest.py
    python daily_digest.py --hours 12
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import (  # noqa: E402
    DIRECTION_EMOJI,
    MARKET_DIRECTION_LABEL,
    bold,
    escape_html,
    fetch_dicts,
    fetch_one_dict,
    mark_notified,
    resolve_telegram_credentials,
)
from common.db import get_connection  # noqa: E402
from config import (  # noqa: E402
    DIGEST_MAX_ITEMS_PER_SECTION,
    DIGEST_SUMMARY_CHARS,
)
from telegram_client import send_telegram_message  # noqa: E402
from telegram_notify import METRIC_LABELS  # noqa: E402

# Той самий виняток контейнерної незалежності, що expectations_notify.py
# (reporting/CLAUDE.md) — expectation_comparisons позначається нотифікованим
# через власний mark_notified() з analysis/expectations/_db.py, не через
# спільний _common.py (той обмежений білим списком таблиць news_*/market_*/
# candidate_assets).
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis")
)
from expectations._db import mark_notified as mark_expectations_notified  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def fetch_recent_news_synthesis(conn, hours: int) -> list[dict]:
    """Усе за вікно, НЕЗАЛЕЖНО від notified_at (зміна 2026-10-13).

    Було `AND notified_at IS NULL` — "дайджест підбирає те, що окремі
    notify не встигли". На живих даних це означало, що він не
    підбирає НІЧОГО: десять окремих notify-джоб проходять раніше й
    позначають усе надісланим. Вивід 12 жовтня — о 20:00 дайджест
    надіслав "Сьогодні суттєвих подій не було" в день, коли Brent
    зробив +5% на ракетному удару по Ер-Ріяду, агент сам написав про
    це 4-джерельний синтез, знайшов ризик short squeeze і завів CPI
    на середу.

    Дайджест — це ЗВЕДЕННЯ дня (Ціль 5: "раз на добу кілька СПРАВДІ
    важливих подій"), а не черга доставки залишків. Повтор того, що
    вже проходило вранці, у вечірньому зведенні — нормальний і
    очікуваний; порожнє зведення в насичений день — ні."""
    return fetch_dicts(
        conn,
        """
        SELECT id, asset_id, cluster_count, net_lean, price_pct_change,
               price_start_date, price_end_date, direction, confidence,
               summary, confirmation_factors
        FROM news_synthesis
        WHERE created_at >= now() - (%s || ' hours')::interval
        ORDER BY created_at DESC
        """,
        (hours,),
    )


def fetch_recent_market_synthesis(conn, hours: int) -> dict | None:
    """Усе за вікно, незалежно від notified_at — той самий принцип,
    що fetch_recent_news_synthesis() (2026-10-13)."""
    return fetch_one_dict(
        conn,
        """
        SELECT id, cluster_count, direction, confidence, summary
        FROM market_synthesis
        WHERE created_at >= now() - (%s || ' hours')::interval
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (hours,),
    )


def fetch_recent_surprises(conn, hours: int) -> list[dict]:
    """Тільки impact_level high/medium — той самий фільтр, що вже в
    expectations_notify.py. Незалежно від notified_at — той самий
    принцип, що fetch_recent_news_synthesis() (2026-10-13)."""
    return fetch_dicts(
        conn,
        """
        SELECT ec.id, ec.metric_id, ec.observed_at, ec.actual_value, ec.expected_value_raw,
               ec.expected_value_parsed, ec.surprise, ec.surprise_pct
        FROM expectation_comparisons ec
        JOIN release_log rl ON rl.id = ec.release_log_id
        WHERE ec.created_at >= now() - (%s || ' hours')::interval
          AND rl.impact_level IN ('high', 'medium')
        ORDER BY ec.created_at DESC
        """,
        (hours,),
    )


def fetch_recent_candidates(conn, hours: int) -> list[dict]:
    """НОВІ рядки candidate_assets за вікно — не весь поточний список,
    тільки те, що з'явилось за --hours (docs/decisions.md, "Ціль 3").
    Незалежно від notified_at — той самий принцип, що
    fetch_recent_news_synthesis() (2026-10-13)."""
    return fetch_dicts(
        conn,
        """
        SELECT id, ticker, company_name, reasoning, direction, asset_class
        FROM candidate_assets
        WHERE discovered_at >= now() - (%s || ' hours')::interval
        ORDER BY discovered_at DESC
        """,
        (hours,),
    )


def format_news_synthesis_message(row: dict) -> str:
    """2026-10-02, живий фідбек користувача: без періоду й
    confirmation_factors повідомлення було нерозбірливим "ціна -5%,
    новини +4" без жодного пояснення. Дати й гіпотеза-що-перевірити
    вже рахувались (price_start_date/end_date, confirmation_factors —
    LLM-поле поруч із summary), просто не доходили до самого
    повідомлення."""
    emoji = DIRECTION_EMOJI.get(row["direction"], "❓")
    lines = [
        bold(
            f"{emoji} {row['asset_id']}: ціна {float(row['price_pct_change']):+.2f}% "
            f"({row['price_start_date']} → {row['price_end_date']}), "
            f"новини {row['net_lean']:+d} ({row['cluster_count']} історій)"
        ),
        escape_html(row["summary"]),
    ]
    if row.get("confirmation_factors"):
        lines.append(f"🔎 Перевірити: {escape_html(row['confirmation_factors'])}")
    return "\n".join(lines)


def format_market_synthesis_message(row: dict) -> str:
    label = MARKET_DIRECTION_LABEL.get(row["direction"], row["direction"])
    confidence = float(row["confidence"])
    header = f"🌍 Стан ринку: {label} (упевненість {confidence:.2f})"
    return f"{bold(header)}\n{escape_html(row['summary'])}"


def format_surprise_message(row: dict) -> str:
    label = METRIC_LABELS.get(row["metric_id"], row["metric_id"])
    pct_suffix = f" ({row['surprise_pct']:+.1f}%)" if row["surprise_pct"] is not None else ""
    header = f"📈 {label} — {row['observed_at']}"
    actual = f"{float(row['actual_value']):.4g}"
    return (
        f"{bold(header)}\n"
        f"Факт {bold(actual)} vs очікування "
        f"{float(row['expected_value_parsed']):.4g} (прогноз: {escape_html(row['expected_value_raw'])})\n"
        f"Сюрприз: {float(row['surprise']):+.4g}{pct_suffix}"
    )


# Стрілка напрямку руху (не рекомендація) — той самий словник, що
# candidates_notify.py; дайджест показує той самий рядок коротше.
_ARROW = {"up": "↑", "down": "↓"}


def format_candidate_message(row: dict) -> str:
    arrow = _ARROW.get(row.get("direction"), "")
    header = f"🆕 {arrow} {row['ticker']} — {row['company_name']}".replace("  ", " ")
    return f"{bold(header)}\n{escape_html(row['reasoning'])}"



def _trim(value, limit: int = None) -> str:
    """Скоротити текст до межі, не обриваючи слово посередині."""
    limit = limit or DIGEST_SUMMARY_CHARS
    text = (value or "").strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut}…"


def build_digest(
    market_row,
    news_rows: list[dict],
    surprise_rows: list[dict],
    candidate_rows: list[dict],
    hours: int,
) -> tuple[str, int]:
    """ОДНЕ повідомлення-зведення з розділами, не потік окремих.

    Ціль 5 просить "раз на добу кілька СПРАВДІ важливих подій".
    Попередні дві реалізації промахувались в обидві сторони: спершу
    дайджест слав "подій не було" (читав лише ненадіслане, а notify-джоби
    забирали все раніше), потім — 25 окремих повідомлень (фільтр
    прибрали, ліміту не було). Повне обґрунтування й живі числа —
    config.py, розділ 15.

    Повертає (текст, кількість позицій). Нуль позицій — caller шле
    контрольне повідомлення."""
    cap = DIGEST_MAX_ITEMS_PER_SECTION
    lines: list[str] = [bold(f"📋 Зведення за {hours}г"), ""]
    count = 0

    if market_row is not None:
        label = MARKET_DIRECTION_LABEL.get(market_row["direction"], market_row["direction"])
        lines.append(f"{bold('Ринок')} — {escape_html(label)} "
                     f"(упевненість {market_row['confidence']}, "
                     f"історій {market_row['cluster_count']})")
        lines.append(escape_html(_trim(market_row["summary"])))
        lines.append("")
        count += 1

    if surprise_rows:
        # Сюрпризи факт/очікування — ПЕРШИМИ серед списків: це єдиний
        # розділ, що описує вже СТАЛУ подію з числом, а не оцінку.
        lines.append(bold(f"📊 Сюрпризи факт/очікування ({len(surprise_rows)})"))
        for row in surprise_rows[:cap]:
            lines.append(
                f"• {escape_html(row['metric_id'])}: факт {row['actual_value']}, "
                f"очікували {escape_html(str(row['expected_value_raw']))}"
            )
            count += 1
        if len(surprise_rows) > cap:
            lines.append(f"  …ще {len(surprise_rows) - cap}")
        lines.append("")

    if news_rows:
        lines.append(bold(f"🔍 Ціна↔новини ({len(news_rows)})"))
        for row in news_rows[:cap]:
            emoji = DIRECTION_EMOJI.get(row["direction"], "")
            change = row.get("price_pct_change")
            change_text = f" {change:+.2f}%" if change is not None else ""
            lines.append(f"• {emoji} {escape_html(row['asset_id'])}{change_text} — "
                         f"{escape_html(_trim(row['summary'], 120))}")
            count += 1
        if len(news_rows) > cap:
            lines.append(f"  …ще {len(news_rows) - cap}")
        lines.append("")

    if candidate_rows:
        lines.append(bold(f"🆕 Новачки ({len(candidate_rows)})"))
        shown = candidate_rows[:cap]
        lines.append("• " + ", ".join(escape_html(r["ticker"]) for r in shown))
        count += len(shown)
        if len(candidate_rows) > cap:
            lines.append(f"  …ще {len(candidate_rows) - cap}")
        lines.append("")

    lines.append("ℹ️ Зведення вже надісланого за добу, не нові сигнали.")
    return "\n".join(lines).strip(), count


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        news_rows = fetch_recent_news_synthesis(conn, args.hours)
        market_row = fetch_recent_market_synthesis(conn, args.hours)
        surprise_rows = fetch_recent_surprises(conn, args.hours)
        candidate_rows = fetch_recent_candidates(conn, args.hours)

        # Дайджест БІЛЬШЕ НЕ ПОЗНАЧАЄ рядки надісланими (2026-10-13).
        # Раніше він це робив, бо був "чергою залишків"; тепер він
        # ЗВЕДЕННЯ дня й нічого не споживає — інакше він відбирав би
        # матеріал у наступних прогонів окремих notify-джоб, тобто
        # просто переносив би проблему в інший бік.
        text, item_count = build_digest(
            market_row=market_row,
            news_rows=news_rows,
            surprise_rows=surprise_rows,
            candidate_rows=candidate_rows,
            hours=args.hours,
        )

        if item_count == 0:
            send_telegram_message(token, chat_id, "Сьогодні суттєвих подій не було.")
            logger.info("Надіслано в Telegram: контрольне повідомлення (нічого за вікно)")
            return

        send_telegram_message(token, chat_id, text, parse_mode="HTML")
        logger.info(
            "Надіслано в Telegram: дайджест за %dг, одне повідомлення, %d позицій",
            args.hours, item_count,
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
