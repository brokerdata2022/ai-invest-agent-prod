#!/usr/bin/env python3
"""
Сповіщення в Telegram про поточні ціни watchlist-активів (золото,
срібло, нафта WTI/Brent, кава, EUR/USD, USD/JPY, BTC, ETH, SOL —
docs/watchlist.md). Тільки форматування вже зібраних цін
(reporting/CLAUDE.md) — жодної аналітики.

Раніше для watchlist не було зведеного звіту — лише
telegram_notify.py --metric <id> по ОДНОМУ показнику за раз (9
окремих команд, щоб побачити весь список). Той самий формат виводу,
що вже є в screening_notify.py: актив, ціна, % зміна — але тут період
зміни ПОКАЗАНО явно й РЕАЛЬНИЙ (не завжди "24г" — див.
fetch_watchlist_snapshot()), бо на відміну від акцій тут різні джерела
мають різну частоту (щоденна FRED-ціна не оновлюється у вихідні,
кава — взагалі місячна серія).

Список активів — живий, з таблиці watchlist_assets
(common/watchlist_db.py, 2026-10-03, редагується через Telegram), не
хардкод — той самий модуль, що вже читають data-ingestion/orchestration
(не analysis/, тож reporting/CLAUDE.md "контейнерна незалежність" не
порушується: common/watchlist_db.py — проста функція читання спільної
таблиці, не бізнес-логіка analysis/).

Без дедупу notified_at (на відміну від screening_notify.py) — це
ЗНІМОК поточного стану (як сам telegram_notify.py), не подія з
таблиці типу "щось нового з'явилось": показати ту саму ціну вдруге
при повторному ручному запуску — очікувана поведінка, не помилка.

Використання:
    python watchlist_notify.py
"""

import argparse
import logging
from datetime import date as date_cls
from decimal import Decimal
from typing import Optional

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import bold, escape_html, resolve_telegram_credentials  # noqa: E402
from common.db import fetch_recent, get_connection  # noqa: E402
from common.freshness import is_stale as _is_stale  # noqa: E402
from common.quality import has_volatile_recent_revisions  # noqa: E402
from common.watchlist_db import fetch_watchlist  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Критичне правило 7 (CLAUDE.md, 2026-10-04): "свіжість даних —
# предмет постійної перевірки, не припущення" — живий фідбек
# користувача показав, що це правило БУЛО сформульоване, але не
# ПЕРЕВІРЯЛОСЬ активно: WTI/Brent/natgas (усі три — товарні FRED-серії)
# застрягли на 2026-09-29 одночасно, і це виявлялось лише вручну, по
# одному активу за раз, після прямої скарги.
#
# Поріг — common/freshness.py:is_stale() (БУДНІ дні, не календарні;
# 2026-10-04, доповнення: "три дні це багато, дані потрібні останні на
# сьогодні якщо ринки відкриті" — фіксовані 3 КАЛЕНДАРНІ дні пропускали
# 2-денну застарілість на буднях, спільний поріг з
# analysis/news_analysis/prices.py, щоб не розходились непомітно).
#
# common/quality.py:has_volatile_recent_revisions() (2026-10-04, живий
# кейс xauusd — Twelve Data forex-OTC переписав ВЖЕ закритий день
# заднім числом на 2%, звіт цього НЕ показував, бо актив був "свіжим").
# Користувач: "як я буду розуміти, чи дані в звіті правильні — кожен
# раз перевіряти вручну?! сенс з такого агента" — тому перевірка не
# ручна (WebFetch у сесії), а автоматична, з уже наявних ревізій у
# raw_observations, прямо в цьому звіті, щоразу.


def fetch_watchlist_snapshot(conn) -> list[dict]:
    """Остання ціна + %-зміна між двома останніми спостереженнями
    (найновіше перше — fetch_recent()) для кожного watchlist-активу
    (common/watchlist_db.py:fetch_watchlist). Актив без жодної точки
    просто пропускається (щойно підключене джерело чи тимчасовий збій
    збору) — не падає на весь список через один.

    `change_days` — РЕАЛЬНИЙ розрив у днях між цими двома точками, не
    завжди 1 (2026-10-04, живий фідбек користувача): FRED-товари/
    форекс не оновлюються у вихідні (п'ятниця -> понеділок = 3 дні),
    кава — взагалі МІСЯЧНА серія (prices.py:ASSET_PRICE_SOURCES
    докстрінг) — різниця там регулярно 28-31 день. Раніше format_message()
    підписував КОЖЕН актив як "24г" незалежно від цього розриву —
    місячний стрибок кави показувався як денна зміна, вводячи в оману
    щодо того, наскільки різкий насправді рух."""
    rows = []
    for row in fetch_watchlist(conn):
        asset_id, source, metric_id, label = row["asset_id"], row["source"], row["metric_id"], row["label"]
        series = fetch_recent(conn, source, metric_id, limit=2)
        if not series:
            continue
        latest = series[0]
        change_pct = None
        change_days = None
        if len(series) >= 2 and series[1]["value"]:
            change_pct = (latest["value"] - series[1]["value"]) / series[1]["value"] * Decimal("100")
            change_days = (latest["observed_at"] - series[1]["observed_at"]).days
        staleness_days = (date_cls.today() - latest["observed_at"]).days
        rows.append(
            {
                "asset_id": asset_id,
                "label": label,
                "price": latest["value"],
                "observed_at": latest["observed_at"],
                "change_pct": change_pct,
                "change_days": change_days,
                "source": source,
                "staleness_days": staleness_days,
                "is_stale": _is_stale(latest["observed_at"]),
                "is_volatile": has_volatile_recent_revisions(conn, source, metric_id),
            }
        )
    return rows


def _format_price(value: Decimal) -> str:
    """>=100 -- 2 знаки (BTC/золото/нафта), <100 -- 4 знаки (форекс-
    пари типу EUR/USD, де 2 знаки губили б увесь рух)."""
    value_f = float(value)
    decimals = 2 if abs(value_f) >= 100 else 4
    return f"{value_f:,.{decimals}f}"


def _format_change(change_pct: Optional[Decimal], change_days: Optional[int]) -> str:
    """Підпис періоду — РЕАЛЬНА кількість днів між двома точками, не
    завжди "24г" (2026-10-04, докстрінг fetch_watchlist_snapshot): "24г"
    лишається лише коли розрив справді 1 день, інакше "N дн." (вихідні
    для FRED-товарів/форексу, ~30 для місячної кави) — без цього різкий
    рух за місяць/вихідні виглядав би як щоденна зміна."""
    if change_pct is None:
        return "н/д"
    if change_days is None:
        period = "н/д"
    elif change_days == 1:
        period = "24г"
    else:
        period = f"{change_days} дн."
    return f"{period}: {float(change_pct):+.2f}%"


def format_message(rows: list[dict]) -> str:
    if not rows:
        return "💰 Дані watchlist-активів ще не зібрані."

    stale_rows = [row for row in rows if row.get("is_stale")]
    volatile_rows = [row for row in rows if row.get("is_volatile")]

    lines = [bold("💰 Watchlist — поточні ціни"), ""]
    for row in rows:
        change_str = _format_change(row.get("change_pct"), row.get("change_days"))
        observed_at = row.get("observed_at")
        date_str = f" ({observed_at.isoformat()})" if observed_at else ""
        # web_crosscheck/tradingeconomics (2026-10-04, rule 7 CLAUDE.md,
        # живий фідбек користувача: "потрібно робити позначку що дані
        # веб") — значення не з офіційного структурованого API, а зі
        # скрапінгу (tradingeconomics_adapter.py) чи ручного крос-чеку
        # (common/manual_observation.py) — позначка відрізняє їх від
        # решти рядків з офіційних API.
        web_marker = " 🌐" if row.get("source") in {"web_crosscheck", "tradingeconomics"} else ""
        # ⚠️ (2026-10-04, common/freshness.py:is_stale() вище) — активна
        # перевірка актуальності ПРЯМО у звіті, не припущення, що дата в
        # дужках сама впаде в очі: живий кейс — WTI/Brent/natgas тижнями
        # показувались із датою 5+ днів тому, і це помітив лише
        # користувач, не сам звіт.
        stale_marker = f" ⚠️ застаріло ({row['staleness_days']}дн)" if row.get("is_stale") else ""
        # "остання сесія" (2026-10-06, живий фідбек користувача: "ціна
        # кави застаріла, чого не застосований резерв") — дата ПОЗАДУ
        # сьогоднішньої, але В МЕЖАХ допуску `is_stale()`. Це не
        # застарілість, а останнє закриття ринку, який не торгується
        # 24/7: кава (ICE) має сесію, тоді як крипта йде 24/7, а
        # нафта й форекс — майже безперервно, тому в них дата вже
        # сьогоднішня. Без цієї позначки звіт показував голу дату, і
        # "ринок закритий" виглядало РІВНО так само, як "наш збір
        # відстав" — користувач прочитав це як збій, і був правий, що
        # відрізнити неможливо.
        session_marker = (
            " · остання сесія"
            if not row.get("is_stale") and (row.get("staleness_days") or 0) >= 1
            else ""
        )
        # 🔀 (2026-10-04, common/quality.py:has_volatile_recent_revisions())
        # — ІНШИЙ сигнал за ⚠️: не "старе", а "те саме минуле значення
        # недавно само переписалось заднім числом на суттєву величину"
        # (живий кейс xauusd) — довіряти цьому числу варто обережніше.
        volatile_marker = " 🔀 нестабільні ревізії" if row.get("is_volatile") else ""
        lines.append(
            f"• {bold(row['label'])}{web_marker} — {_format_price(row['price'])}{date_str} "
            f"| {change_str}{session_marker}{stale_marker}{volatile_marker}"
        )

    if stale_rows:
        names = ", ".join(row["label"] for row in stale_rows)
        lines.append("")
        lines.append(f"⚠️ Застарілі дані: {escape_html(names)}")

    if volatile_rows:
        names = ", ".join(row["label"] for row in volatile_rows)
        lines.append("")
        lines.append(f"🔀 Нестабільні ревізії (перевірте вручну): {escape_html(names)}")

    return "\n".join(lines).rstrip()


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()  # лише для --help/валідації: скрипт без параметрів

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        rows = fetch_watchlist_snapshot(conn)
    finally:
        conn.close()

    if not rows:
        logger.info("Жодного watchlist-активу з даними — сповіщення не надсилається")
        return

    text = format_message(rows)
    send_telegram_message(token, chat_id, text, parse_mode="HTML")
    logger.info("Надіслано в Telegram: watchlist, %d активів", len(rows))


if __name__ == "__main__":
    main()
