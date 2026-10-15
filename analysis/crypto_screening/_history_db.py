"""
SQL для історії ф'ючерсних рядів (OI/обсяг) по ТРЬОХ біржах одразу.
Окремий файл з тієї самої причини, що решта `_*_db.py` у проєкті: у
`run_screening.py` лишається проводка, у `history.py` — арифметика,
тут — тільки запит.

**Чому пари (source, metric_id), а не regexp по metric_id.** Кожна
біржа зберігає свій рядок під власним написанням символу й власною
назвою метрики:

| джерело | символ | OI | обсяг |
|---|---|---|---|
| `binance_futures` | `btcusdt` | `_open_interest_usd` ¹ | `_quote_volume` |
| `bybit_futures` | `btcusdt` | `_open_interest_value` | `_quote_volume` |
| `okx_futures` | `btc-usdt-swap` | `_open_interest_usd` | `_quote_volume` |

¹ Binance не має bulk-OI взагалі — його per-symbol OI зберігає
`run_screening.py` для символів, що пройшли (чи майже пройшли) Tier A
(docs/decisions.md 2026-10-15). Покриття тут НЕРІВНОМІРНЕ, і це
нормально: `history.consistent_daily_series()` бере лише біржі,
присутні на обох краях вікна, тож частковий ряд не створює фальшивого
руху.

Явний перелік пар тримає запит на наявному індексі
`idx_raw_observations_lookup (source, metric_id, ...)`. Нормалізація
`metric_id` регуляркою в WHERE дала б послідовне сканування всього
ф'ючерсного шматка на КОЖЕН символ (їх ~150 на прогін) — той самий
клас проблеми, що виправлений N+1 у Tier A акцій (docs/decisions.md
2026-09-25).
"""

from datetime import date, timedelta

from crypto_screening.aggregate_sources import okx_symbol

FUTURES_SOURCES = ("binance_futures", "bybit_futures", "okx_futures")

# Назва $-метрики OI по біржах (див. docstring модуля).
_OI_SUFFIX_BY_SOURCE = {
    "binance_futures": "open_interest_usd",
    "bybit_futures": "open_interest_value",
    "okx_futures": "open_interest_usd",
}
_VOLUME_SUFFIX = "quote_volume"

KIND_OI = "oi"
KIND_VOLUME = "volume"


def metric_pairs(canonical_symbol: str, kind: str) -> tuple:
    """[(source, metric_id)] для всіх бірж, де такий ряд МОЖЕ існувати.
    OKX пропускається для не-USDT символів (`okx_symbol()` вертає None —
    ця біржа збирає лише USDT-SWAP)."""
    if kind not in (KIND_OI, KIND_VOLUME):
        raise ValueError(f"невідомий kind: {kind!r}")

    pairs: list[tuple] = []
    for source in FUTURES_SOURCES:
        if source == "okx_futures":
            symbol = okx_symbol(canonical_symbol)
            if symbol is None:
                continue
        else:
            symbol = canonical_symbol
        suffix = _OI_SUFFIX_BY_SOURCE[source] if kind == KIND_OI else _VOLUME_SUFFIX
        pairs.append((source, f"{symbol.lower()}_{suffix}"))
    return tuple(pairs)


def fetch_history_rows(conn, canonical_symbol: str, kind: str, days: int) -> list[tuple]:
    """Останні `days` днів ряду по всіх біржах: [(observed_at, source,
    value)], найновіше перше.

    Читає `v_observations_latest_revision`, не `raw_observations` — для
    кожної дати лишається тільки остання ревізія (той самий принцип, що
    `common/db.py:fetch_recent`, інакше ряд містив би шум старих
    ревізій того самого дня)."""
    pairs = metric_pairs(canonical_symbol, kind)
    if not pairs:
        return []

    # Межа ДАТИ — обчислена в Python, НЕ `current_date` у SQL. Зміряно
    # 2026-10-15 на 1211 чанках: із літеральною датою TimescaleDB
    # відсікає чанки ще на плануванні (planning 1877 мс -> 13.5 мс,
    # execution 45 мс -> 0.7 мс), а `current_date` як STABLE-функція
    # цього не дає — планувальник перебирає всі чанки. На прогоні
    # скринінгу це ~450 запитів, тобто різниця між 14 хвилинами
    # чистого планування й кількома секундами.
    #
    # Побічно це ще й ГЕЙТ СВІЖОСТІ: ряд, збір якого спинився місяць
    # тому, більше не дає "зміну за 7 днів" зі старих точок — той
    # самий урок, що централізований staleness-гейт для цін
    # (docs/decisions.md 2026-10-04).
    cutoff = date.today() - timedelta(days=days)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, source, value
            FROM v_observations_latest_revision
            WHERE (source, metric_id) IN %s
              AND observed_at >= %s
            ORDER BY observed_at DESC
            LIMIT %s
            """,
            # Межа рядків — днів × бірж: один день може дати до одного
            # рядка на біржу, і зрізати вікно раніше, ніж це зробить
            # consistent_daily_series(), не можна.
            (pairs, cutoff, days * len(pairs)),
        )
        return cur.fetchall()
