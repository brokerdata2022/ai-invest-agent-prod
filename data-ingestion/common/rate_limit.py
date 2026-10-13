"""
Глобальний (МІЖПРОЦЕСНИЙ) обмежувач швидкості запитів до зовнішнього
API.

Чому не `time.sleep()` у скрипті (саме це тут і було, і саме це не
працювало — docs/decisions.md, 2026-10-05): три джоби збору новин
стартують у межах кількох хвилин, кожна як ОКРЕМИЙ процес. Пауза в
одному процесі нічого не знає про запити двох інших, тож сумарний
потік усе одно перевищував ліміт GDELT (1 запит/5с), IP потрапляв у
штрафний стан, і всі три джоби отримували 429 на першому ж запиті.

Серіалізація — `pg_advisory_xact_lock` + рядок із часом останнього
запиту. Процес, що хоче зробити запит, тримає лок, доки не відлежить
решту інтервалу, тож у кожен момент часу запит до API робить лише
один процес у всій системі (app і scheduler — різні контейнери, але
БД одна).

Свідома простота: лок тримається під час sleep. Для інтервалів у
кілька секунд це саме те, що потрібно (черга запитів), а не проблема —
альтернатива "відпустити лок і поспати" дала б гонку, через яку два
процеси прокинулись би одночасно.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Callable

logger = logging.getLogger(__name__)


def throttle(
    conn,
    api: str,
    min_interval_seconds: float,
    max_wait_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> float:
    """Чекає, доки від останнього запиту до `api` (будь-яким процесом)
    пройде `min_interval_seconds`, і реєструє поточний запит. Повертає
    фактично проспаний час у секундах (0.0 — чекати не довелось).

    `max_wait_seconds` — стеля очікування: захист від зсунутого
    годинника чи рядка з майбутнім часом (інакше джоба зависла б
    назавжди замість зробити запит).

    Викликається ПЕРЕД КОЖНИМ HTTP-запитом, включно з повторами після
    429 — інакше retry-цикл знову обходив би обмежувач (це й був
    механізм самопідсилення збою)."""
    with conn.cursor() as cur:
        # Транзакційний advisory lock — звільняється самим commit нижче,
        # навіть якщо процес упаде (на відміну від pg_advisory_lock,
        # який жив би до кінця сесії й міг би заблокувати решту).
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (api,))

        cur.execute("SELECT last_request_at FROM api_rate_limit WHERE api = %s", (api,))
        row = cur.fetchone()

        waited = 0.0
        if row is not None and row[0] is not None:
            elapsed = (datetime.now(timezone.utc) - row[0]).total_seconds()
            remaining = min_interval_seconds - elapsed
            if remaining > 0:
                waited = min(remaining, max_wait_seconds)
                logger.info("%s: чекаю %.1fс до наступного запиту (глобальний ліміт)", api, waited)
                sleep(waited)

        cur.execute(
            """
            INSERT INTO api_rate_limit (api, last_request_at)
            VALUES (%s, now())
            ON CONFLICT (api) DO UPDATE SET last_request_at = now()
            """,
            (api,),
        )
    conn.commit()
    return waited


def make_pacer(
    conn,
    api: str,
    min_interval_seconds: float,
    max_wait_seconds: float,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[], None]:
    """Готовий колбек для адаптера: `pacer()` перед кожним запитом.

    Адаптер не знає ні про БД, ні про те, як саме рахується інтервал
    (rule 2, CLAUDE.md — адаптер лише дістає й нормалізує дані); він
    лише кличе `pacer()` перед кожним HTTP-запитом."""

    def pacer() -> None:
        throttle(conn, api, min_interval_seconds, max_wait_seconds, sleep)

    return pacer
