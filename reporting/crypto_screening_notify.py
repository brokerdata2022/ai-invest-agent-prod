#!/usr/bin/env python3
"""
Сповіщення в Telegram про крипто-кандидатів SHORT/WATCH
(crypto_screening_candidates) — вже пораховано
analysis/crypto_screening/run_screening.py (денний скан, заводить
кандидата при пампі ≥30%) + monitor_candidates.py (погодинне оновлення
статусу). Тільки форматування готового результату, жодної аналітики
(reporting/CLAUDE.md).

docs/archive/plan-journal-2026-10-11.md, Фаза 4, Крок 5: "Telegram-сповіщення про SHORT/WATCH (зараз
лише лог)" — закрито тут. LONG-кандидати (той самий run_screening.py,
широкий скан ринку) НЕ покриті — вони лише логуються, ніколи не
персистуються в БД (окреме рішення, потребує власної таблиці — не
зроблено цією сесією).

**Без дедупу notified_at — ПОВНИЙ активний список щоразу (рішення
користувача, 2026-10-03, dev-версія):** живий кейс показав, що дедуп
"лише нове/змінене" (попередня версія цього скрипта) ховав уже
активних кандидатів (MAGMAUSDT/SANDUSDT лишались 'candidate' і не
показувались повторно, хоча й далі пампили) — для короткоживучого
спостереження за пампами користувачу потрібен ПОВНИЙ поточний список
кожного разу, не лише дельта. **В продакшені це рішення планується
переглянути** (можливо повернути дедуп, можливо інший підхід) — див.
docs/decisions.md, 2026-10-03. Колонка `notified_at` і скидання при
зміні статусу (_candidates_db.py:update_candidate()) лишені в БД
незачепленими — просто не читаються й не пишуться тут, щоб дедуп
можна було повернути пізніше без міграції схеми.

Використання (після run_screening.py і/або monitor_candidates.py):
    python crypto_screening_notify.py
"""

import argparse
import logging

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import (  # noqa: E402
    bold, composition_changed, escape_html, fetch_dicts,
    remember_composition, resolve_telegram_credentials,
)
from common.db import get_connection  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Ключ для _common.composition_changed — дорівнює назві джоби.
NOTIFIER = "notify_crypto_screening"

# Тон: описові характеристики (напрямок ризику, рівень уваги), не
# прямі інструкції "шорти/купуй" (analysis/CLAUDE.md "Заборонені
# формулювання" — той самий принцип стосується й reporting/).
STATUS_LABEL = {
    "short": "🔴 SHORT-нахил",
    "watch": "🟡 WATCH (спостереження, не напрямок сигналу)",
    "candidate": "🟠 кандидат — памп тримається, критерії ще не підтвердились",
}


def fetch_active_crypto_candidates(conn) -> list[dict]:
    """УСІ активні (не closed) кандидати, кожен виклик — не лише нові/
    змінені (2026-10-03, рішення користувача — docstring модуля). SHORT
    і WATCH першими (найважливіші), потім candidate."""
    return fetch_dicts(
        conn,
        """
        SELECT id, symbol, status, pump_pct_at_detection, last_pump_pct,
               last_funding_rate, reason, detected_at
        FROM crypto_screening_candidates
        WHERE status != 'closed'
        ORDER BY
            CASE status WHEN 'short' THEN 0 WHEN 'watch' THEN 1 ELSE 2 END,
            detected_at DESC
        """,
    )


def format_message(rows: list[dict]) -> str:
    if not rows:
        return "🪙 Активних крипто-кандидатів SHORT/WATCH немає."

    lines = [bold(f"🪙 Крипто-скринінг ф'ючерсів — {len(rows)} кандидатів:"), ""]
    for row in rows:
        label = STATUS_LABEL.get(row["status"], row["status"])
        pump = row["last_pump_pct"] if row["last_pump_pct"] is not None else row["pump_pct_at_detection"]
        lines.append(bold(f"{row['symbol']} — {label}"))
        lines.append(f"• памп {float(pump):+.1f}%")
        if row["last_funding_rate"] is not None:
            lines.append(f"• funding {float(row['last_funding_rate']) * 100:+.3f}%")
        if row["reason"]:
            lines.append(f"• {escape_html(row['reason'])}")
        lines.append("")
    return "\n".join(lines).rstrip()


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()  # лише для --help/валідації: скрипт без параметрів

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        rows = fetch_active_crypto_candidates(conn)
        if not rows:
            logger.info("Активних крипто-кандидатів немає — сповіщення не надсилається")
            return

        # Цей скрипт СВІДОМО не має дедупу по рядках — шле повний
        # активний список щоразу (рішення 2026-10-03). Рішення
        # лишається чинним: перелік має бути повним. Але повторювати
        # НЕЗМІННИЙ перелік 4 рази на добу (джоба о 0/6/12/18) — це
        # шум; живий вивід 12 жовтня показав той самий 1 кандидат
        # чотири рази. Тепер повний перелік ідe лише тоді, коли його
        # СКЛАД змінився: з'явився новий кандидат або закрився старий.
        symbols = [f'{row["symbol"]}:{row["status"]}' for row in rows]
        if not composition_changed(conn, NOTIFIER, symbols):
            logger.info(
                "Склад активних крипто-кандидатів не змінився (%d) — не надсилаємо",
                len(symbols),
            )
            return

        text = format_message(rows)
        send_telegram_message(token, chat_id, text, parse_mode="HTML")
        remember_composition(conn, NOTIFIER, symbols)
        logger.info("Надіслано в Telegram: %d крипто-кандидатів", len(rows))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
