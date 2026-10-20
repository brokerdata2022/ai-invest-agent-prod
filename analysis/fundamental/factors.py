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
  кодом;
- `"asset:<asset_id>"` — ціна ІНШОГО активу watchlist через його
  поточне джерело (чек-листи від ШІ, `fundamental/checklist.py`).

Для факторних рядів (`config.FACTOR_SERIES`) до чинника додається
позначка довіри — звірено / одне джерело / розійшлись / застаріло.

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
from typing import TYPE_CHECKING, Optional

import config
from common.freshness import is_stale

if TYPE_CHECKING:
    from fundamental.checklist import Checklist


@dataclass
class Factor:
    """Один чинник впливу з уже дістаними даними."""

    key: str
    label: str
    points: list[dict] = field(default_factory=list)   # [{observed_at, value}]
    texts: list[str] = field(default_factory=list)      # для news/synthesis
    missing_reason: Optional[str] = None                # чому даних немає
    # Позначка довіри для факторних рядів (config.FACTOR_SERIES):
    # звірено / одне джерело / розійшлись / застаріло.
    note: Optional[str] = None


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


def _fetch_asset_price_source(conn, asset_id: str) -> Optional[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, metric_id FROM watchlist_assets WHERE asset_id = %s AND enabled",
            (asset_id,),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else None


def _fetch_latest_verdict(conn, metric_id: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT observed_at, status, max_spread_pct, source_values, detail
            FROM factor_verdicts
            WHERE metric_id = %s
            ORDER BY observed_at DESC
            LIMIT 1
            """,
            (metric_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(("observed_at", "status", "max_spread_pct", "source_values", "detail"), row))


def trust_note(metric_id: str, verdict: Optional[dict], last_observed_at: Optional[date],
               today: Optional[date] = None) -> str:
    """Позначка довіри до факторного ряду для промпта: чи підтвердило
    значення друге джерело і чи воно свіже.

    Навіщо: без неї LLM однаково довіряв би звіреним запасам EIA і
    потокам ETF з одного скрапінгу, а застиглий ряд читав би як
    "нічого не змінюється" — рівно той клас тихих дефектів, що
    `docs/decisions.md` 2026-10-13 описує як головний у проєкті."""
    spec = config.FACTOR_SERIES[metric_id]
    parts: list[str] = []

    threshold = spec["stale_business_days"]
    if last_observed_at is not None and threshold is not None and is_stale(
        last_observed_at, today=today, threshold_business_days=threshold
    ):
        parts.append(f"ЗАСТАРІЛО: остання точка {last_observed_at}")

    if not spec["secondary"]:
        parts.append(f"одне джерело ({spec['primary']}), незалежної звірки немає")
    elif verdict is None:
        parts.append("звірки ще не було")
    else:
        others = ", ".join(item["source"] for item in spec["secondary"])
        status = verdict["status"]
        if status == "agree":
            spread = verdict.get("max_spread_pct")
            spread_text = f", розкид {Decimal(spread):.2f}%" if spread is not None else ""
            parts.append(f"звірено з {others} на {verdict['observed_at']}: згода{spread_text}")
        elif status in ("outlier", "disagree"):
            values = verdict.get("source_values") or {}
            rendered = ", ".join(f"{s}={v}" for s, v in values.items())
            parts.append(f"ДЖЕРЕЛА РОЗІЙШЛИСЬ на {verdict['observed_at']}: {rendered}")
        else:
            parts.append(f"звірка не вдалась: {verdict.get('detail') or status}")

    return "; ".join(parts)


def _factor_series_metric(key: str) -> Optional[str]:
    """metric_id факторного ряду, якщо ключ — його ОСНОВНЕ джерело."""
    if key.count(":") != 1:
        return None
    source, metric_id = key.split(":", 1)
    spec = config.FACTOR_SERIES.get(metric_id)
    if spec is None or spec["primary"] != source:
        return None
    return metric_id


def collect_asset_factors(
    conn, asset_id: str, label: str, price_source: str, price_metric_id: str,
    checklist: Optional["Checklist"] = None,
) -> AssetFactors:
    """Усі чинники одного активу за його чек-листом.

    `checklist` — уже вирішений чек-лист (`fundamental/checklist.py`:
    ручний із config, складений ШІ або універсальний). Без нього —
    ручний із `config.ASSET_FACTORS` або універсальний набір.

    Чинник без даних НЕ відкидається, а лишається з `missing_reason` —
    щоб у промпт пішло явне "цього чинника немає", а не тиша: LLM
    інакше не знає, чи чинник неважливий, чи просто не дістався
    (той самий принцип честних прогалин, що `ASSET_FACTOR_GAPS`)."""
    if checklist is not None:
        specs = checklist.specs
        using_default = checklist.origin == "default"
    else:
        specs = config.ASSET_FACTORS.get(asset_id)
        using_default = specs is None
    if specs is None:
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
        elif key.startswith("asset:"):
            # Ціна іншого активу watchlist — через його ПОТОЧНЕ джерело
            # (воно автоматично перемикається), не через прибите в
            # чек-листі (fundamental/checklist.py).
            other_id = key[len("asset:"):]
            other = _fetch_asset_price_source(conn, other_id)
            if other is None:
                factor.missing_reason = f"активу {other_id} немає в списку обраних"
            else:
                factor.points = _fetch_series(conn, other[0], other[1], config.FACTOR_HISTORY_POINTS)
                if not factor.points:
                    factor.missing_reason = f"ціна {other_id} не збирається ({other[0]}/{other[1]})"
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
            series_metric = _factor_series_metric(key)
            if series_metric and factor.points:
                factor.note = trust_note(
                    series_metric,
                    _fetch_latest_verdict(conn, series_metric),
                    date.fromisoformat(factor.points[-1]["observed_at"]),
                )
        else:
            factor.missing_reason = f"невідомий тип чинника {key!r}"

        factors.append(factor)

    if checklist is not None:
        gaps = tuple(checklist.gaps)
    else:
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
