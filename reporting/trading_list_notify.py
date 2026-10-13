#!/usr/bin/env python3
"""
Надсилає в Telegram список активів, які варто РОЗГЛЯДАТИ для торгівлі
(`trading_list` — `analysis/trading_list/`, дизайн
`docs/trading-list.md`).

Не дублює `screening_notify.py` (фундаментальний скринінг акцій),
`crypto_screening_notify.py` (крипто-сетапи) чи `watchlist_notify.py`
(ціни обраних активів): ті показують СВОЇ зрізи, цей — результат
фільтра за каталізатором поверх усіх трьох.

## Повідомлення людською мовою (переписано 2026-10-04)

Живий фідбек користувача на перший варіант: "він не читабильний а
технічний, має бути нормальне пояснення людською мовою а не змішано".
Що було не так і що змінено:

- **Скори наголо** ("0.87 (каталізатор 0.71 · тренд 1.00 · якість
  0.50)") — внутрішні числа для калібрування. Лишились у БД, у
  повідомленні тепер ЯКІСНА сила ("сильний"/"помірний"/"слабкий").
- **`metric_id` замість назв** (`mortgage_rate_30y`) → `METRIC_LABELS`
  з `telegram_notify.py`, уже наявний словник людських підписів.
- **`XAGUSD`/`BRENT_CRUDE`** → `trading_list.label` (там уже лежить
  "Срібло (XAG/USD)", назва компанії для акцій).
- **Той самий реліз кілька разів** → причини ГРУПУЮТЬСЯ за типом і
  показником, не перелічуються як є.
- **"суперечливо" без розшифровки** → тепер явно сказано, ЩО
  суперечить ("тренд вгору, новини вниз").
- **Обрізані на півслові речення** → обрізання по межі слова.

Межа шарів лишається (reporting/CLAUDE.md: тільки представлення):
`analysis/` віддає СТРУКТУРОВАНІ причини (`trading_list.reasons`
JSONB), текст складається тут. Жодного нового обчислення, жодного
пере-ранжування — поріг і топ уже застосовані в
`scoring.py:select_final`.

Формулювання свідомо ОПИСОВІ — напрямок сигналу й причина, без
"купити/продати" (analysis/CLAUDE.md, "Заборонені формулювання").

Використання (після analysis/trading_list/run_trading_list.py):
    python trading_list_notify.py
"""

import argparse
import logging
from decimal import Decimal

from dotenv import load_dotenv

# _common додає data-ingestion у sys.path (дефіс у назві теки —
# не валідне ім'я Python-пакета), тому імпортується ПЕРШИМ.
from _common import (  # noqa: E402
    bold,
    escape_html,
    fetch_dicts,
    mark_notified,
    resolve_telegram_credentials,
)
from common.db import get_connection  # noqa: E402
from telegram_client import send_telegram_message  # noqa: E402
from telegram_notify import METRIC_LABELS  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HORIZON = "medium"

# Межі якісної сили сигналу. Дублюють
# `config.py:STRENGTH_*` свідомо — reporting/ не
# імпортує з analysis/ (reporting/CLAUDE.md, контейнерна
# незалежність), як і METRIC_LABELS дублює підписи показників.
STRENGTH_STRONG = Decimal("0.70")
STRENGTH_MODERATE = Decimal("0.50")

DIRECTION_PHRASE = {
    "up": "🟢 сигнал вгору",
    "down": "🔴 сигнал вниз",
    "neutral": "⚪ без чіткого напрямку",
    "unclear": "❓ напрямок незрозумілий",
    "conflicting": "⚠️ сигнали розходяться",
}

DIRECTION_WORD = {"up": "вгору", "down": "вниз", "neutral": "без напрямку"}

KIND_LABEL = {
    "stock": "акція",
    "crypto": "крипта",
    "fx": "валютна пара",
    "commodity": "товар",
}

# Людські назви джерел причини — для фрази "чому актив у списку".
REASON_SOURCE_WORD = {
    "news": "новини",
    "synthesis": "звʼязок ціни з новинами",
    "trend": "тренд ціни",
    "release_done": "вийшлі дані",
    "release_upcoming": "очікувані дані",
    "forecast": "наш прогноз",
}


def metric_label(metric_id: str) -> str:
    """Людський підпис показника; невідомий id віддаємо як є (перелік
    показників росте, і новий не має ламати повідомлення)."""
    return METRIC_LABELS.get(metric_id, metric_id)


def strength_word(score) -> str:
    value = Decimal(str(score))
    if value >= STRENGTH_STRONG:
        return "сильний"
    if value >= STRENGTH_MODERATE:
        return "помірний"
    return "слабкий"


def trim(text: str, limit: int = 160) -> str:
    """Обрізає по межі СЛОВА, не посередині — живий фідбек 2026-10-04
    (речення обривались на '…' на півслові)."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut}…"


def fetch_unnotified(conn, horizon: str = HORIZON) -> list[dict]:
    """Рядки ПОСЛІДНЬОГО прогону цього горизонту, ще не надіслані.

    Фільтр за `run_at = MAX(run_at)` критичний: без нього повторний
    прогін (новий знімок) надсилав би ще й старі ненадіслані рядки
    попереднього — а вони вже неактуальні."""
    return fetch_dicts(
        conn,
        """
        SELECT id, asset_id, label, kind, direction, score, reasons
        FROM trading_list
        WHERE horizon = %s
          AND notified_at IS NULL
          AND run_at = (SELECT MAX(run_at) FROM trading_list WHERE horizon = %s)
        ORDER BY score DESC
        """,
        (horizon, horizon),
    )


def describe_conflict(reasons: list[dict]) -> str:
    """Що саме розходиться — "тренд вгору, новини вниз".

    Без цього позначка "суперечливо" була марною (живий фідбек
    2026-10-04): користувач бачив попередження, але не бачив, між чим
    саме конфлікт."""
    up_sources, down_sources = [], []
    for reason in reasons:
        # Для релізів беремо НАЗВУ показника, не загальне "вийшлі
        # дані" — інакше виходило "вийшлі дані — вгору, а вийшлі дані
        # — вниз" (живий фідбек 2026-10-04), що нічого не пояснює.
        metric_id = reason.get("metric_id")
        if metric_id and reason.get("kind", "").startswith("release"):
            word = metric_label(metric_id)
        else:
            word = REASON_SOURCE_WORD.get(reason.get("kind", ""), reason.get("kind", ""))

        if reason.get("direction") == "up" and word not in up_sources:
            up_sources.append(word)
        elif reason.get("direction") == "down" and word not in down_sources:
            down_sources.append(word)

    if not up_sources or not down_sources:
        return ""
    return f"{', '.join(up_sources)} — вгору, а {', '.join(down_sources)} — вниз"


def explain(reasons: list[dict]) -> list[str]:
    """Причини людською мовою, згруповані за типом.

    Групування обовʼязкове: один реліз дає кілька записів (по категорії
    впливу), і перелічувати їх як є — саме те, що робило повідомлення
    нечитабельним."""
    lines: list[str] = []

    upcoming = {r.get("metric_id"): r for r in reasons if r.get("kind") == "release_upcoming"}
    if upcoming:
        parts = [
            f"{metric_label(mid)} ({r.get('when')})"
            for mid, r in upcoming.items() if mid
        ]
        if parts:
            lines.append(f"📅 Попереду: {'; '.join(parts)}")

    done = {}
    for reason in reasons:
        if reason.get("kind") == "release_done" and reason.get("metric_id"):
            done.setdefault(reason["metric_id"], reason)
    if done:
        parts = []
        for mid, reason in done.items():
            direction = DIRECTION_WORD.get(reason.get("direction") or "", "")
            suffix = f" → {direction}" if direction and direction != "без напрямку" else ""
            parts.append(f"{metric_label(mid)}{suffix}")
        lines.append(f"📊 Вийшли дані: {'; '.join(parts)}")

    for reason in reasons:
        if reason.get("kind") == "news" and reason.get("detail"):
            count = reason.get("when") or "кілька"
            lines.append(f"📰 Новини ({count} джерел): {trim(reason['detail'])}")
            break

    for reason in reasons:
        if reason.get("kind") == "synthesis" and reason.get("detail"):
            lines.append(f"🔗 {trim(reason['detail'])}")
            break

    forecasts = {r.get("metric_id"): r for r in reasons if r.get("kind") == "forecast"}
    for mid, reason in forecasts.items():
        # `detail` — напрямок самого ПОКАЗНИКА; `direction` це голос про
        # актив і для чужої категорії завжди neutral (через що раніше
        # виходило "наш прогноз CPI — без напрямку" у 8 рядках із 10).
        word = DIRECTION_WORD.get(reason.get("detail") or "", "")
        if mid and word and word != "без напрямку":
            lines.append(f"🔮 Наш прогноз {metric_label(mid)} — {word}")
        break

    for reason in reasons:
        if reason.get("kind") == "trend" and reason.get("detail"):
            lines.append(f"📈 Тренд: {trim(reason['detail'], 90)}")
            break

    return lines


def _reason_key(reason: dict) -> tuple:
    return (reason.get("kind"), reason.get("metric_id"), reason.get("direction"))


def macro_background(rows: list[dict]) -> list[dict]:
    """Макро-події з усіх рядків — ОБʼЄДНАННЯ, не перетин.

    Живий фідбек 2026-10-04: "Вийшли дані: ..." і "Попереду: ..."
    дослівно повторювались у всіх 10 рядках. Перша спроба виділяла
    СПІЛЬНІ причини (перетин) — і не спрацювала, бо набори справді
    різні: в акцій є `initial_jobless_claims`, у крипти немає, та й
    напрямок того самого релізу відрізняється (власна категорія
    активу). Макро-реліз за визначенням фон, тож у шапку йде
    обʼєднання всіх подій у вікні, по одному разу на показник."""
    seen: set[tuple[str, str]] = set()
    background: list[dict] = []
    for row in rows:
        for reason in row.get("reasons") or []:
            kind = str(reason.get("kind", ""))
            if kind not in ("release_upcoming", "release_done", "forecast"):
                continue
            key = (kind, str(reason.get("metric_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            # Напрямок у фоні не показуємо: він різний для різних
            # активів (та сама подія "вгору" для золота й "вниз" для
            # валюти), тож у шапці лишається сама ПОДІЯ.
            background.append({**reason, "direction": "neutral"})
    return background


def asset_specific(reasons: list[dict]) -> list[dict]:
    """Причини, що несуть інформацію САМЕ про цей актив.

    Відкидаються: заплановані релізи й прогноз (вони вже у фоні в
    шапці) і вийшлі релізи БЕЗ напрямку для цього активу — остання
    категорія це рівно те, що робило 6 акцій дослівно однаковими."""
    kept = []
    for reason in reasons:
        kind = str(reason.get("kind", ""))
        if kind in ("release_upcoming", "forecast"):
            continue
        if kind == "release_done" and reason.get("direction") in (None, "", "neutral"):
            continue
        kept.append(reason)
    return kept


def format_message(rows: list[dict]) -> str:
    lines = [
        bold(f"🎯 Активи для розгляду — {len(rows)}"),
        "<i>Середньостроковий горизонт: дні-тижні</i>",
        "",
    ]

    background = macro_background(rows)
    if background:
        lines.append(bold("📌 Макро-фон"))
        for line in explain(background):
            lines.append(f"   {escape_html(line)}")
        lines.append("")

    for i, row in enumerate(rows, start=1):
        name = row.get("label") or row["asset_id"]
        kind = KIND_LABEL.get(row["kind"], row["kind"])
        phrase = DIRECTION_PHRASE.get(row["direction"], row["direction"])
        strength = strength_word(row["score"])

        lines.append(f"{i}. {bold(name)} · {escape_html(kind)}")
        lines.append(f"{phrase}, {escape_html(strength)}")

        all_reasons = row.get("reasons") or []
        # У рядку активу — лише те, що САМЕ про нього; спільний фон уже
        # в шапці. Суперечність рахуємо по ПОВНОМУ набору: конфлікт між
        # фоном і трендом активу теж реальний.
        reasons = asset_specific(all_reasons)
        if row["direction"] == "conflicting":
            conflict = describe_conflict(all_reasons)
            if conflict:
                lines.append(f"   ⚠️ {escape_html(conflict)}")

        reason_lines = explain(reasons)
        if reason_lines:
            for reason_line in reason_lines:
                lines.append(f"   {escape_html(reason_line)}")
        else:
            # Своїх причин немає — у списку актив лише завдяки
            # макро-фону, тренду й фундаменталу. Казати це прямо
            # честніше, ніж лишати порожній рядок.
            lines.append("   <i>власних новин немає — тренд і макро-фон</i>")

        lines.append("")

    lines.append("<i>ℹ️ Перелік для розгляду з напрямком сигналу, не вказівка діяти.</i>")
    return "\n".join(lines)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--horizon", default=HORIZON)
    args = parser.parse_args()

    token, chat_id = resolve_telegram_credentials()

    conn = get_connection()
    try:
        rows = fetch_unnotified(conn, horizon=args.horizon)
        if not rows:
            logger.info("Нових активів у торговому списку немає — не надсилаємо")
            return

        send_telegram_message(token, chat_id, format_message(rows), parse_mode="HTML")
        mark_notified(conn, "trading_list", [row["id"] for row in rows])
        logger.info("Надіслано в Telegram: %d активів", len(rows))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
