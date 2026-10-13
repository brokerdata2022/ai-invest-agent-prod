"""
Збирання ЧИННИКІВ впливу по одному активу — читання з БД за
декларативним чек-листом із `config.py:ASSET_FACTORS`.

Рішення користувача 2026-10-04: "кожний актив аналізується окремо
згідно їхніх чинників". Аналіз золота за показниками нафти безглуздий,
тож єдиного набору входів бути не може — кожен актив має свій.

Що тут НЕ відбувається: жодної інтерпретації. Модуль лише дістає
числа й новинний фон за списком ключів; що вони означають — задача
LLM (`fundamental_llm.py`).

Формат ключа чинника (`config.py`):
- `"source:metric_id"` — ряд із `raw_observations`;
- `"price"` — власна ціна активу, джерело береться з
  `watchlist_assets` (тож переживає автоматичне перемикання джерела,
  `common/watchlist_db.py:choose_freshest_source`);
- `"news"` / `"synthesis"` — новинний фон і готовий звʼязок
  ціна↔новини по цьому активу;
- `"yoy:source:metric_id"` — РІЧНА зміна у відсотках, порахована
  кодом.

## Чому існує `yoy:` (живий дефект, 2026-10-04)

Перший прогін аналізу золота дав хибну реальну ставку: у промпт
пішов РІВЕНЬ індексу CPI (334.1 проти 330.3), і LLM вивів із цього
"річну інфляцію ~1.1%" — хоча це зміна за ПʼЯТЬ МІСЯЦІВ, не за рік.
Далі він відняв це від дохідності 10Y і отримав реальну ставку 4.1%,
якої не існує.

Корінь — не в моделі, а в тому, ЩО я їй дав: для чинника "реальна
дохідність" потрібна річна зміна, а рівень індексу вимагає обчислення,
яке LLM робити не має (`analysis/CLAUDE.md`: не використовуйте LLM
для того, що можна порахувати формулою). Тепер `yoy:` рахує її
детерміновано, і в промпт іде готовий відсоток.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import config


@dataclass
class Factor:
    """Один чинник впливу з уже дістаними даними."""

    key: str
    label: str
    points: list[dict] = field(default_factory=list)   # [{observed_at, value}]
    texts: list[str] = field(default_factory=list)      # для news/synthesis
    missing_reason: Optional[str] = None                # чому даних немає


@dataclass
class AssetFactors:
    asset_id: str
    label: str
    factors: list[Factor]
    gaps: tuple[str, ...] = ()
    using_default_factors: bool = False

    @property
    def available(self) -> list[Factor]:
        return [f for f in self.factors if f.points or f.texts]

    @property
    def unavailable(self) -> list[Factor]:
        return [f for f in self.factors if not (f.points or f.texts)]


def _fetch_series(conn, source: str, metric_id: str, limit: int) -> list[dict]:
    """Ряд у ХРОНОЛОГІЧНОМУ порядку з `v_observations_latest_revision`
    (не з `raw_observations`): для кожної дати лишається остання
    відома ревізія, інакше в чинник потрапляв би шум старих ревізій."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, value
            FROM v_observations_latest_revision
            WHERE source = %s AND metric_id = %s
            ORDER BY observed_at DESC
            LIMIT %s
            """,
            (source, metric_id, limit),
        )
        rows = [{"observed_at": str(r[0]), "value": r[1]} for r in cur.fetchall()]
    return list(reversed(rows))


# Допуск при пошуку точки "рік тому": місячні/квартальні серії
# рідко мають рівно ту саму дату, тож беремо найближчу в межах вікна.
# 20 днів — той самий допуск, що `screening/tier_b.py:YOY_TOLERANCE_DAYS`.
_YOY_TOLERANCE_DAYS = 20


def _fetch_yoy(conn, source: str, metric_id: str) -> list[dict]:
    """РІЧНА зміна у відсотках — порахована КОДОМ, не LLM.

    Повертає список з одного елемента (щоб форма збігалась із
    звичайним рядом), де `value` — відсоток, а `observed_at` — дата
    свіжішої точки. Порожній список, якщо немає пари точок із
    різницею ~рік (честно, без підміни іншим періодом)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, value
            FROM v_observations_latest_revision
            WHERE source = %s AND metric_id = %s
            ORDER BY observed_at DESC
            LIMIT 400
            """,
            (source, metric_id),
        )
        rows = cur.fetchall()

    if not rows:
        return []

    latest_date, latest_value = rows[0]
    target = latest_date - timedelta(days=365)
    best = None
    for observed_at, value in rows[1:]:
        delta = abs((observed_at - target).days)
        if delta <= _YOY_TOLERANCE_DAYS and (best is None or delta < best[0]):
            best = (delta, observed_at, value)

    if best is None or not best[2]:
        return []

    _, base_date, base_value = best
    pct = (Decimal(latest_value) - Decimal(base_value)) / abs(Decimal(base_value)) * 100
    return [{
        "observed_at": f"{base_date} → {latest_date}",
        "value": f"{pct:+.2f}%",
    }]


def _fetch_news(conn, asset_id: str, limit: int = 3) -> list[str]:
    """Консолідовані новини по активу — найважливіші за `source_count`
    (механічна міра важливості, рішення 2026-09-28)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT summary, source_count
            FROM news_consolidated
            WHERE asset_id = %s
              AND created_at >= now() - make_interval(hours => %s)
            ORDER BY source_count DESC, created_at DESC
            LIMIT %s
            """,
            (asset_id, config.CATALYST_NEWS_HOURS, limit),
        )
        return [f"[{row[1]} джерел] {row[0]}" for row in cur.fetchall()]


def _fetch_synthesis(conn, asset_id: str) -> list[str]:
    """Готовий LLM-звʼязок ціна↔новини (`news_synthesis`) — не
    перераховуємо, а переюзуємо вже зроблений аналіз."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT summary, direction, confidence
            FROM news_synthesis
            WHERE asset_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (asset_id,),
        )
        row = cur.fetchone()
    if not row:
        return []
    return [f"[напрямок {row[1]}, впевненість {float(row[2]):.2f}] {row[0]}"]


def collect_asset_factors(
    conn, asset_id: str, label: str, price_source: str, price_metric_id: str
) -> AssetFactors:
    """Усі чинники одного активу за його чек-листом.

    Чинник без даних НЕ відкидається, а лишається з `missing_reason` —
    щоб у промпт пішло явне "цього чинника немає", а не тиша: LLM
    інакше не знає, чи чинник неважливий, чи просто не дістався
    (той самий принцип честних прогалин, що `ASSET_FACTOR_GAPS`)."""
    specs = config.ASSET_FACTORS.get(asset_id)
    using_default = specs is None
    if using_default:
        # Актив, доданий через `/watchlist_add` без свого чек-листа
        # (живий кейс `bnb`, 2026-10-04) — беремо універсальний набір,
        # а не випадаємо з аналізу назовсім: watchlist редагується
        # через Telegram БЕЗ зміни коду, і аналіз має це витримувати.
        specs = config.DEFAULT_ASSET_FACTORS

    factors: list[Factor] = []

    for key, factor_label in specs:
        factor = Factor(key=key, label=factor_label)

        if key == "price":
            factor.points = _fetch_series(
                conn, price_source, price_metric_id, config.FACTOR_HISTORY_POINTS
            )
            if not factor.points:
                factor.missing_reason = (
                    f"ціна не збирається ({price_source}/{price_metric_id})"
                )
        elif key == "news":
            factor.texts = _fetch_news(conn, asset_id)
            if not factor.texts:
                factor.missing_reason = "значимих новин за вікно не було"
        elif key == "synthesis":
            factor.texts = _fetch_synthesis(conn, asset_id)
            if not factor.texts:
                factor.missing_reason = "синтезу ціна/новини ще немає"
        elif key.startswith("yoy:"):
            source, metric_id = key[4:].split(":", 1)
            factor.points = _fetch_yoy(conn, source, metric_id)
            if not factor.points:
                factor.missing_reason = (
                    f"немає пари точок із різницею ~рік у {source}/{metric_id}"
                )
        elif ":" in key:
            source, metric_id = key.split(":", 1)
            factor.points = _fetch_series(
                conn, source, metric_id, config.FACTOR_HISTORY_POINTS
            )
            if not factor.points:
                factor.missing_reason = f"немає даних у {source}/{metric_id}"
        else:
            factor.missing_reason = f"невідомий тип чинника {key!r}"

        factors.append(factor)

    gaps = config.ASSET_FACTOR_GAPS.get(asset_id, ())
    if using_default:
        # Честно попереджаємо в тому самому блоці, що й решта прогалин:
        # загальний аналіз не має подаватись як повний.
        gaps = gaps + (config.DEFAULT_FACTORS_CAVEAT,)

    return AssetFactors(
        asset_id=asset_id,
        label=label,
        factors=factors,
        gaps=gaps,
        using_default_factors=using_default,
    )
