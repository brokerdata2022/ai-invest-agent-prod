"""
Ручний (не адаптерний) запис спостереження — ОСТАННІЙ резерв для
watchlist-активу, для якого НЕМАЄ взагалі жодного автоматизованого
джерела — ні структурованого API, ні скрапінг-адаптера типу
`commodities/tradingeconomics_adapter.py` (critical rule 7, CLAUDE.md;
2026-10-04, рішення користувача: "мені не потрібні фікси на окремі
дані, мені потрібний один працюючий код" — живий урок: coffee/
wti_crude/brent_crude/natgas пройшли через цей механізм лише
ТИМЧАСОВО, протримавшись кілька годин сесії, доки не знайшовся
справжній автоматизований tradingeconomics-скрапінг, на який їх
перемкнуто). source="web_crosscheck" (db/schema.sql) — значення має
бути звірене МІНІМУМ на 2 незалежних фінансових сайтах перед записом,
source_refs обов'язковий (аудит, той самий принцип, що rule 5
CLAUDE.md для LLM-викликів).

НЕ адаптер (common/adapter.py:BaseAdapter) — немає fetch() до
зовнішнього API, значення приходить ЗЗОВНІ (людина чи LLM-веб-пошук),
ця функція лише валідує мінімум source_refs і зберігає через той самий
common/db.py:insert_observation(), що й усі адаптери (той самий
append-only/revision контракт, rule 6 CLAUDE.md). Тому й НЕ викликається
з жодної джоби APScheduler (orchestration/schedule.py) — оновлення
вручну, за потреби.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from common.adapter import NormalizedRecord
from common.db import insert_observation

SOURCE = "web_crosscheck"


def insert_crosschecked_observation(
    conn,
    metric_id: str,
    value: Decimal,
    observed_at: date,
    source_refs: list[str],
) -> Optional[int]:
    """`source_refs` — МІНІМУМ 2 URL (чи інших людиночитних посилань на
    джерело), з яких узгоджено значення — без цього запис відмовляється
    (аудит постфактум неможливий без хоча б двох точок звірки, той
    самий принцип, що вже є для LLM-висновків, rule 5)."""
    if len(source_refs) < 2:
        raise ValueError(
            f"Потрібно МІНІМУМ 2 source_refs (звірка на 2+ сайтах), отримано {len(source_refs)}: {source_refs!r}"
        )

    record = NormalizedRecord(
        source=SOURCE,
        metric_id=metric_id,
        value=value,
        observed_at=observed_at,
        fetched_at=datetime.now(timezone.utc),
        revision=None,
        raw_payload={"source_refs": source_refs},
    )
    return insert_observation(conn, record)
