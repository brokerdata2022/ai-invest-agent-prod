"""
SQL журналу наслідків: які рішення чекають на оцінку, де взяти ціну
на дату й куди записати вердикт.

**Звідки беруться «рішення».** Чотири джерела, кожне з власним
написанням активу й власною колонкою часу:

| таблиця | kind у журналі | ключ активу | де ціна |
|---|---|---|---|
| `trading_list` | `trading_list` | `asset_id` | залежить від `source` |
| `crypto_long_candidates` | `crypto_long` | `symbol` | Binance klines |
| `crypto_screening_candidates` | `crypto_watch` | `symbol` | Binance klines |
| `candidate_assets` | `candidate` | `ticker` | БД або klines |

**Чому «одне рішення на актив на добу».** `trading_list` і крипто-LONG
пишуться 3-4 рази на добу, і той самий актив у той самий день — це
ОДНЕ рішення, повторене прогоном. Без дедупу hit rate зважувався б
частотою джоби, а не якістю рішень (той самий урок, що
`notify_fingerprints`: дедупити треба СКЛАД, не рядки).

**Чому ціни беруться з різних місць.** Ф'ючерсних цін у нас у БД немає
взагалі — `crypto_derivatives_collect` зберігає обсяг/funding/OI, а
ціна в скринінгу завжди бралась живими klines. Акції й watchlist,
навпаки, у БД є. Тому `venue` обчислюється разом із рішенням, а не
вгадується потім.
"""

from datetime import date, timedelta
from typing import Optional

VENUE_DB_STOCK = "db_stock"
VENUE_DB_WATCHLIST = "db_watchlist"
VENUE_BINANCE_FUTURES = "binance_futures"


def _dicts(cur) -> list[dict]:
    columns = [d[0] for d in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _venue_for_trading_list(kind: str, source: str) -> str:
    """`trading_list` змішує класи: watchlist-активи лежать у БД, а
    символи з крипто-скринінгу — ні."""
    if source == "crypto_screening":
        return VENUE_BINANCE_FUTURES
    if source == "screening" or kind == "stock":
        return VENUE_DB_STOCK
    return VENUE_DB_WATCHLIST


def fetch_pending_decisions(conn, lookback_days: int, horizon_days: int) -> list[dict]:
    """Рішення, старші за горизонт, яких ще немає в `decision_outcomes`.

    Межа «не молодші за горизонт» — у SQL, а не в Python: рішення,
    зроблене сьогодні, оцінювати нічим, і тягнути його з БД немає
    сенсу."""
    oldest = date.today() - timedelta(days=lookback_days)
    newest = date.today() - timedelta(days=horizon_days)
    decisions: list[dict] = []

    with conn.cursor() as cur:
        # ── 1. Список торгів: найраніший рядок на (актив, добу) ──
        cur.execute(
            """
            SELECT DISTINCT ON (asset_id, run_at::date)
                   id, asset_id, kind, source, direction, run_at
            FROM trading_list
            WHERE run_at::date BETWEEN %s AND %s
            ORDER BY asset_id, run_at::date, run_at
            """,
            (oldest, newest),
        )
        for row in _dicts(cur):
            decisions.append({
                "decision_kind": "trading_list",
                "decision_id": row["id"],
                "asset_key": row["asset_id"],
                "asset_class": row["kind"],
                "direction": row["direction"],
                "decided_at": row["run_at"],
                "venue": _venue_for_trading_list(row["kind"], row["source"]),
            })

        # ── 2. Крипто-LONG: те саме дедуплювання по добі ──
        cur.execute(
            """
            SELECT DISTINCT ON (symbol, run_at::date) id, symbol, run_at
            FROM crypto_long_candidates
            WHERE run_at::date BETWEEN %s AND %s
            ORDER BY symbol, run_at::date, run_at
            """,
            (oldest, newest),
        )
        for row in _dicts(cur):
            decisions.append({
                "decision_kind": "crypto_long",
                "decision_id": row["id"],
                "asset_key": row["symbol"],
                "asset_class": "crypto",
                "direction": "long",
                "decided_at": row["run_at"],
                "venue": VENUE_BINANCE_FUTURES,
            })

        # ── 3. Крипто SHORT/WATCH: рядок мутабельний, дедуп не потрібен ──
        cur.execute(
            """
            SELECT id, symbol, status, detected_at
            FROM crypto_screening_candidates
            WHERE detected_at::date BETWEEN %s AND %s
            """,
            (oldest, newest),
        )
        for row in _dicts(cur):
            decisions.append({
                "decision_kind": "crypto_watch",
                "decision_id": row["id"],
                "asset_key": row["symbol"],
                "asset_class": "crypto",
                # 'short' — твердження про падіння; 'watch'/'candidate' —
                # спостереження без напрямку (outcomes.NO_CLAIM).
                "direction": "short" if row["status"] == "short" else row["status"],
                "decided_at": row["detected_at"],
                "venue": VENUE_BINANCE_FUTURES,
            })

        # ── 4. Новачки: вже унікальні (ticker, доба) за індексом ──
        cur.execute(
            """
            SELECT id, ticker, asset_class, direction, discovered_at
            FROM candidate_assets
            WHERE discovered_at::date BETWEEN %s AND %s
            """,
            (oldest, newest),
        )
        for row in _dicts(cur):
            decisions.append({
                "decision_kind": "candidate",
                "decision_id": row["id"],
                "asset_key": row["ticker"],
                "asset_class": row["asset_class"],
                "direction": row["direction"],
                "decided_at": row["discovered_at"],
                "venue": (
                    VENUE_BINANCE_FUTURES if row["asset_class"] == "crypto" else VENUE_DB_STOCK
                ),
            })

        # ── Уже оцінені — відкидаємо ──
        cur.execute(
            "SELECT decision_kind, decision_id FROM decision_outcomes WHERE horizon_days = %s",
            (horizon_days,),
        )
        done = {(r[0], r[1]) for r in cur.fetchall()}

    return [d for d in decisions if (d["decision_kind"], d["decision_id"]) not in done]


def watchlist_series(conn, asset_id: str) -> Optional[tuple]:
    """(source, metric_id) активного ряду watchlist-активу — джерело
    могло автоматично перемкнутись (`common/watchlist_db.py`), тому
    питаємо БД, а не вгадуємо."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, metric_id FROM watchlist_assets WHERE asset_id = %s",
            (asset_id,),
        )
        return cur.fetchone()


def close_on_or_before(conn, source: str, metric_id: str, target: date) -> Optional[tuple]:
    """Остання відома ціна НА або ДО дати — вихідні й свята інакше
    давали б діру замість котирування.

    Межа «не глибше 7 днів» захищає від протилежної помилки: взяти
    ціну місячної давнини за «ціну на дату рішення» означало б
    порахувати рух, якого не було (той самий урок, що централізований
    staleness-гейт, docs/decisions.md 2026-10-04)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, value
            FROM v_observations_latest_revision
            WHERE source = %s AND metric_id = %s
              AND observed_at <= %s AND observed_at >= %s
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (source, metric_id, target, target - timedelta(days=7)),
        )
        return cur.fetchone()


def save_outcome(conn, outcome: dict) -> None:
    """UPSERT по (kind, id, horizon): повторний прогін того самого дня
    не має створювати другий рядок на те саме рішення."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO decision_outcomes (
                decision_kind, decision_id, asset_key, asset_class, direction,
                decided_at, horizon_days, price_at_decision, price_at_eval,
                realized_pct, verdict, detail, evaluated_at
            ) VALUES (
                %(decision_kind)s, %(decision_id)s, %(asset_key)s, %(asset_class)s,
                %(direction)s, %(decided_at)s, %(horizon_days)s, %(price_at_decision)s,
                %(price_at_eval)s, %(realized_pct)s, %(verdict)s, %(detail)s, now()
            )
            ON CONFLICT (decision_kind, decision_id, horizon_days) DO UPDATE SET
                price_at_decision = EXCLUDED.price_at_decision,
                price_at_eval     = EXCLUDED.price_at_eval,
                realized_pct      = EXCLUDED.realized_pct,
                verdict           = EXCLUDED.verdict,
                detail            = EXCLUDED.detail,
                evaluated_at      = now()
            """,
            outcome,
        )
    conn.commit()


def fetch_hit_rates(conn, horizon_days: int, since_days: int) -> list[dict]:
    """Зведення по видах рішень за вікно — те, що читає звіт."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT decision_kind,
                   count(*)                                            AS total,
                   count(*) FILTER (WHERE verdict = 'correct')         AS correct,
                   count(*) FILTER (WHERE verdict = 'incorrect')       AS incorrect,
                   count(*) FILTER (WHERE verdict = 'flat')            AS flat,
                   count(*) FILTER (WHERE verdict = 'no_claim')        AS no_claim,
                   count(*) FILTER (WHERE verdict = 'unavailable')     AS unavailable,
                   avg(realized_pct) FILTER (WHERE verdict IN ('correct', 'incorrect')) AS avg_move
            FROM decision_outcomes
            WHERE horizon_days = %s
              AND evaluated_at >= now() - (%s || ' days')::interval
            GROUP BY decision_kind
            ORDER BY decision_kind
            """,
            (horizon_days, since_days),
        )
        return _dicts(cur)
