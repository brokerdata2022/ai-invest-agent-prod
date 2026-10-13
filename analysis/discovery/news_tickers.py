"""
Детектор новачків із новин — ЄДИНИЙ детектор у `discovery/`, що
викликає LLM, і роль LLM тут СВІДОМО ЗВУЖЕНА до екстракції сутностей:
"про які публічно торговані компанії ця історія".

Чому звужена (docs/decisions.md, 2026-10-05). Попередня реалізація
(`news_analysis/discover_candidates.py`) просила LLM ВИГАДАТИ
перспективну ідею — "запропонуй 3-5 акцій, які виглядають
перспективними" — і за весь час дала **0 рядків**: це найважче, що
можна попросити (треба й знати ринок, і не вигадати тикер, і не
назвати те, що вже відстежуємо), а general-потік новин забитий
IPO-шумом, який промпт сам же й забороняв пропонувати. Екстракція —
рівно те, у чому LLM надійний: назва компанії в тексті вже є, її
треба лише дістати й перекласти в тикер.

Детерміноване підтвердження лишається незмінним (той самий принцип
"LLM пропонує, код перевіряє", analysis/CLAUDE.md): Twelve Data
повинна віддати реальний ряд цін для тикера, інакше кандидат
відкидається. Напрямок кандидата — з ЦІНИ (рух за вікно), не зі слів
LLM.
"""

import logging
from decimal import Decimal
from typing import Optional

import requests

from common.db import insert_observations
from config import (
    DISCOVERY_NEWS_MAX_VERIFY,
    DISCOVERY_NEWS_SOURCE_REFERENCE,
    DISCOVERY_STOCK_MIN_POINTS,
    DISCOVERY_STOCK_SATURATION_PCT,
    DISCOVERY_STOCK_SHORT_DAYS,
    DISCOVERY_VOLUME_SPIKE_REFERENCE,
    DISCOVERY_W_MOVE,
    DISCOVERY_W_NEWS,
    DISCOVERY_W_VOLUME,
)
from llm_common import parse_json_object
from quotes.twelvedata_adapter import TwelveDataAdapter

from discovery.signals import (
    Candidate,
    direction_from_move,
    score_candidate,
    volume_spike_pct,
)
from discovery.stock_movers import window_move_pct

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ти витягуєш назви публічно торгованих компаній із фінансових "
    "новин. Це завдання на ЕКСТРАКЦІЮ, не на прогноз: не оцінюй "
    "перспективи, не пропонуй ідей, не вигадуй компаній, яких у "
    "тексті немає."
    "\n\n"
    "Для кожної історії назви компанії, ПРО ЯКІ вона прямо йдеться, і "
    "їхні біржові тикери. Правила:"
    "\n"
    "- тільки компанії, акції яких торгуються на біржі зараз; ніколи "
    "не приватні стартапи й не чутки про майбутнє IPO;"
    "\n"
    "- тікер — такий, як на біржі (напр. NVDA, ASML, TSM); якщо "
    "точного тикера не знаєш — не вигадуй, пропусти компанію;"
    "\n"
    "- не повторюй тикери зі списку 'уже відстежуємо' нижче;"
    "\n"
    "- якщо в історії немає жодної конкретної публічної компанії — "
    "просто не згадуй її; порожній список це нормальний результат."
    "\n\n"
    "НІКОЛИ не давай торгових рекомендацій ('купити'/'продати') — "
    "поля для цього немає. Відповідай ЛИШЕ JSON-об'єктом з полем "
    "'tickers' — списком об'єктів з полями: ticker, company_name, "
    "story (номер історії зі списку нижче, число), mention (коротко, "
    "що саме в новині стосується компанії)."
)


class NewsTickerResponseError(ValueError):
    pass


def build_prompt(clusters: list, already_tracked: list[str]) -> str:
    lines = [
        f"Уже відстежуємо (НЕ повторювати): {', '.join(sorted(already_tracked))}",
        "",
        f"Історії за вікно ({len(clusters)}), пронумеровані:",
    ]
    for index, cluster in enumerate(clusters, start=1):
        lines.append(f"{index}. [{cluster.source_count} джерел] {cluster.representative_title}")
        if cluster.summaries:
            lines.append(f"   → {cluster.summaries[0]}")
    return "\n".join(lines)


def parse_response(raw_content: str) -> list[dict]:
    """Власний парсер (не `llm_common.parse_synthesis_response`): форма
    виходу тут інша — список витягнутих сутностей, а не один
    direction/confidence/summary/reasoning."""
    data = parse_json_object(raw_content, NewsTickerResponseError)

    tickers = data.get("tickers")
    if tickers is None:
        raise NewsTickerResponseError(f"У відповіді LLM бракує поля 'tickers': {data!r}")
    if not isinstance(tickers, list):
        raise NewsTickerResponseError(f"'tickers' має бути списком: {tickers!r}")

    for item in tickers:
        missing = [f for f in ("ticker", "company_name") if not item.get(f)]
        if missing:
            raise NewsTickerResponseError(f"Витягнутому тикеру бракує полів {missing}: {item!r}")

    return tickers


def verify_tradable(
    conn, ticker: str, twelvedata_api_key: str, limit: int = 30
) -> tuple[list[Decimal], list[Decimal]]:
    """Детермінована перевірка "реально торгується" — Twelve Data або
    віддає ряд цін, або ні. Повертає (closes, volumes) у
    ХРОНОЛОГІЧНОМУ порядку; порожні списки = тикер не підтверджено.

    Зібрані бари одразу пишуться в `raw_observations`
    (`insert_observations`) — це ті самі дані, що й у решти
    quotes-збору, і наступного прогону вони вже дають
    `stock_movers` історію без нового запиту до API."""
    try:
        records = TwelveDataAdapter(api_key=twelvedata_api_key, ticker=ticker).collect(limit=limit)
    except (ValueError, requests.exceptions.RequestException):
        logger.info("%s: Twelve Data не визнає тикер — відкинуто", ticker)
        return [], []

    if not records:
        return [], []

    insert_observations(conn, records)

    ordered = sorted(records, key=lambda r: r.observed_at)
    closes = [r.value for r in ordered if r.metric_id.endswith("_close")]
    volumes = [r.value for r in ordered if r.metric_id.endswith("_volume")]
    return closes, volumes


def detect(
    conn,
    clusters: list,
    tracked: set[str],
    extracted: list[dict],
    twelvedata_api_key: str,
) -> list[Candidate]:
    """Верифікує витягнуті LLM тикери й перетворює підтверджені на
    кандидатів. Сам LLM-виклик робить проводка (`run_discovery.py`) —
    так аудит-лог виклику (rule 5) пишеться в одному місці для всіх
    детекторів, а цю функцію можна тестувати без мережі."""
    candidates: list[Candidate] = []
    seen: set[str] = set()

    for item in extracted:
        if len(candidates) >= DISCOVERY_NEWS_MAX_VERIFY:
            logger.info("Ліміт верифікацій (%d) вичерпано", DISCOVERY_NEWS_MAX_VERIFY)
            break

        ticker = str(item["ticker"]).upper().strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        if ticker in tracked:
            logger.info("%s: уже в наших списках — не новачок", ticker)
            continue

        closes, volumes = verify_tradable(conn, ticker, twelvedata_api_key)
        if len(closes) < DISCOVERY_STOCK_MIN_POINTS:
            logger.info("%s: не підтверджено як реально торгований — пропущено", ticker)
            continue

        cluster = _cluster_for(clusters, item.get("story"))
        source_count = cluster.source_count if cluster else 1

        move = window_move_pct(closes, DISCOVERY_STOCK_SHORT_DAYS)
        spike = volume_spike_pct(volumes, DISCOVERY_STOCK_SHORT_DAYS)
        score = score_candidate(
            move_pct=move,
            move_saturation_pct=DISCOVERY_STOCK_SATURATION_PCT,
            w_move=DISCOVERY_W_MOVE,
            w_volume=DISCOVERY_W_VOLUME,
            w_news=DISCOVERY_W_NEWS,
            volume_spike_pct=spike,
            volume_reference_pct=DISCOVERY_VOLUME_SPIKE_REFERENCE,
            news_source_count=source_count,
            news_source_reference=DISCOVERY_NEWS_SOURCE_REFERENCE,
        )

        mention = str(item.get("mention") or "").strip()
        reason_parts = [f"у новинах ({source_count} джерел)"]
        if mention:
            reason_parts.append(mention)
        if move is not None:
            reason_parts.append(f"рух {move:+.1f}% за {DISCOVERY_STOCK_SHORT_DAYS} торгових днів")
        if spike is not None:
            reason_parts.append(f"обсяг до середнього {spike:+.0f}%")

        candidates.append(
            Candidate(
                asset_id=ticker,
                label=str(item["company_name"]),
                asset_class="stock",
                detector="news_tickers",
                # Напрямок — з ЦІНИ, не зі слів LLM: тон новини й
                # фактична реакція ринку розходяться частіше, ніж
                # хотілося б, а перевірити ми можемо лише друге.
                direction=direction_from_move(move),
                reason="; ".join(reason_parts),
                signals={
                    "move_pct_short": move,
                    "volume_spike_pct": spike,
                    "news_source_count": source_count,
                },
                score=score,
                source_refs=(
                    [{"type": "news_cluster", "title": cluster.representative_title,
                      "source_count": cluster.source_count, "urls": cluster.urls[:3]}]
                    if cluster
                    else [{"type": "news_cluster", "title": mention}]
                ),
            )
        )
        logger.info("%s (%s): підтверджено, score=%s", ticker, item["company_name"], score)

    return candidates


def _cluster_for(clusters: list, story: Optional[object]):
    """Історія, на яку посилається LLM (1-based номер у промпті).
    Некоректний/відсутній номер — не помилка прогону: кандидат
    лишається, просто без посилання на кластер (той самий принцип
    м'якої деградації, що решта парсерів у проєкті)."""
    try:
        index = int(story)
    except (TypeError, ValueError):
        return None
    if 1 <= index <= len(clusters):
        return clusters[index - 1]
    return None
