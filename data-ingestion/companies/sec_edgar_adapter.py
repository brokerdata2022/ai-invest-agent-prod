"""
Адаптер SEC EDGAR — фундаментальні факти (revenue, net income, EPS,
shares outstanding, assets, liabilities) для довільного тикера.
https://www.sec.gov/edgar/sec-api-documentation

Офіційне, безкоштовне, без ключа джерело — НЕ виняток з "official
sources first" (на відміну від quotes/twelvedata_adapter.py).
Обов'язковий User-Agent з контактом (SEC блокує 403 без нього),
конструктор називає параметр "api_key" (не "user_agent") — так само,
як EstatAdapter називає його не "app_id" — щоб збігатися з генеричним
викликом у run_collect.py:main() (needs_api_key → api_key=...).
Фактичний вміст — контакт користувача, docs/decisions.md, 2026-09-14:
brokerdata2022@gmail.com.

Архітектурна відмінність від macro/*-адаптерів: METRICS тут —
словник КОНЦЕПТІВ (us-gaap/dei tags), не фіксованих показників
країни. Тикер — параметр конструктора (docs/decisions.md,
2026-09-14, "companies/ — спершу скринінг"). metric_id збирається
як "{ticker}_{concept}", напр. "aapl_revenue".

ВАЖЛИВО: формат відповіді companyconcept підтверджено кількома
незалежними технічними джерелами (включно з дзеркалом офіційної
документації SEC — sec-edgar-api.readthedocs.io), АЛЕ не живим
запитом з цієї сесії — sec.gov немає в дозволених доменах пісочниці
Claude (на відміну від raw.githubusercontent.com, який вдалось
перевірити напряму для collect_universe.py). Перший живий запуск
користувача — реальна перевірка, як і для кожного попереднього
джерела в цьому проєкті.

Відомий ризик, знайдений через минулий баг з japan_cpi (estat_adapter):
XBRL duration-концепти (Revenues тощо) для одного "end"-дня можуть
мати кілька записів з РІЗНИМИ "start" (квартальне значення й
кумулятивне з початку року — обидва закінчуються тим самим днем).
Мовчазне злиття їх під одним (metric_id, observed_at) виглядало б як
фальшиві ревізії — той самий клас багу, що й у e-Stat. Тому
normalize() фільтрує duration-записи за довжиною періоду (~80-100
днів = один квартал), а не бере все підряд.

ДРУГИЙ дублікат-кейс, знайдений живим тестом 2026-09-21 (перший, вище,
задокументовано ще до живого запуску — цей виявився вже постфактум):
навіть у межах одного кварталу (та сама пара start/end) SEC інколи
віддає ДВА записи з різним val — типово після спліту акцій, коли
пізніше подання перераховує EPS минулих періодів під нову к-сть
акцій. У логах це виглядало як "ping-pong" ревізії (1.97→13.81→1.97...).
Виправлено дедуплікацією по даті: з кількох записів на ту саму дату
береться той, що з найпізнішим "filed" (найактуальніше подання).

Відомий пробіл (не вирішено в цій версії): деякі компанії звітують
виручку під тегом `RevenueFromContractWithCustomerExcludingAssessedTax`
замість `Revenues` (пост-ASC 606) — див. config.py.
Якщо основний тег дає 404, revenue для цієї компанії просто не
збереться (без падіння) — автоматичного фолбеку на інший тег поки
немає.
"""

import logging
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json"

# metric_id-суфікс → список варіантів (taxonomy, tag), не один тег.
# Причина: компанії міняють XBRL-тег для того самого економічного
# факту (найчастіше — revenue після ASC 606: старі подання під
# "Revenues", нові під "RevenueFromContractWithCustomerExcludingAssessedTax").
# Один тег на компанію часто покриває тільки частину історії — живий
# тест на AAPL 2026-09-21 показав "Revenues" зупиняється на 2018 році.
# Усі варіанти фетчаться й зливаються в normalize() через ту саму
# дедуплікацію по даті (найпізніший "filed" виграє), що вже є для
# спліт-дублікатів — тут дублікат може виникнути і між тегами, не
# тільки в межах одного тега.
METRICS: dict[str, list[tuple[str, str]]] = {
    "revenue": [
        ("us-gaap", "Revenues"),
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ],
    "net_income": [("us-gaap", "NetIncomeLoss")],
    "eps_diluted": [("us-gaap", "EarningsPerShareDiluted")],
    # Резервні теги додані 2026-10-13. Живий замір: 69 тикерів із 512
    # (COIN, DDOG, APP, CVNA, DELL, ABNB, BIIB, DASH, CTSH, EL…) мовчки
    # випадали з воронки Tier A, бо не мають `shares_outstanding` — а
    # без нього не рахується market cap. Перевірено прямим запитом до
    # SEC для Coinbase (CIK 0001679788):
    #   dei/EntityCommonStockSharesOutstanding      → HTTP 404
    #   us-gaap/CommonStockSharesOutstanding        → HTTP 200
    #   us-gaap/CommonStockSharesIssued             → HTTP 200
    #   us-gaap/WeightedAverageNumberOf...Diluted   → HTTP 200
    # Тобто дані були, адаптер просто питав єдиним тегом.
    #
    # Порядок = спадання точності для market cap:
    # 1. dei — титульна сторінка звіту, найсвіжіша фактична кількість;
    # 2. CommonStockSharesOutstanding — те саме по суті, us-gaap;
    # 3. WeightedAverageNumberOfDilutedSharesOutstanding — СЕРЕДНЯ за
    #    період і РОЗМИТА, тобто завищує відносно фактичної на розмір
    #    опціонів/конвертованих; свідомо остання.
    # `CommonStockSharesIssued` НЕ включено: вона містить викуплені
    # (treasury) акції, тож завищувала б market cap тим сильніше, чим
    # активніший байбек — а це рівно ті компанії, що нас цікавлять.
    "shares_outstanding": [
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "CommonStockSharesOutstanding"),
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
    ],
    "assets": [("us-gaap", "Assets")],
    "liabilities": [("us-gaap", "Liabilities")],
}

# Тільки звітні форми — відсікає шум на кшталт 8-K (одноразові події,
# не регулярна звітність) чи виправлень поза стандартним циклом.
_ALLOWED_FORMS = {"10-K", "10-Q"}

# SEC офіційний ліміт — 10 запитів/сек (fair-access policy).
_SECONDS_BETWEEN_REQUESTS = 0.15

# Duration-концепт (Revenues, NetIncomeLoss, EPS) з "start" вважаємо
# квартальним значенням лише якщо період 75-100 днів — інакше це
# кумулятивне значення з початку року (той самий "end", інший
# "start") і його треба відкинути, а не змішати з квартальним під
# тим самим metric_id/датою.
_MIN_QUARTER_DAYS = 75
_MAX_QUARTER_DAYS = 100

_cik_cache: dict[str, str] = {}  # тикер (upper) -> zero-padded CIK, кеш на процес


class SecEdgarAdapter(BaseAdapter):
    source = "sec_edgar"

    def __init__(
        self,
        api_key: str,
        ticker: str,
        cik: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        if not api_key:
            raise ValueError(
                "SEC_EDGAR_USER_AGENT не задано — SEC блокує запити без "
                "контактного User-Agent (403 Forbidden). Див. .env.example."
            )
        if not ticker or not ticker.strip():
            raise ValueError(f"Некоректний тикер для SEC EDGAR: {ticker!r}")

        self.user_agent = api_key
        self.ticker = ticker.strip().upper()
        self.session = session or requests.Session()
        self.cik = cik.zfill(10) if cik else None  # якщо не дано — резолвиться лениво

    def _resolve_cik(self) -> str:
        if self.cik:
            return self.cik
        if self.ticker in _cik_cache:
            self.cik = _cik_cache[self.ticker]
            return self.cik

        response = self.session.get(
            TICKERS_URL, headers={"User-Agent": self.user_agent}, timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        for row in data.values():
            if str(row.get("ticker", "")).upper() == self.ticker:
                cik = str(row["cik_str"]).zfill(10)
                _cik_cache[self.ticker] = cik
                self.cik = cik
                return cik

        raise ValueError(
            f"Тикер {self.ticker!r} не знайдено в company_tickers.json (SEC EDGAR)."
        )

    def fetch(self, **kwargs) -> Any:
        cik = self._resolve_cik()
        headers = {"User-Agent": self.user_agent}
        results: dict[str, list[dict]] = {}

        is_first_request = True
        for suffix, tag_variants in METRICS.items():
            for taxonomy, tag in tag_variants:
                if not is_first_request:
                    # SEC офіційний ліміт — 10 запитів/сек. 0.15с між
                    # запитами ≈ 6.7/сек, запас на неточність.
                    time.sleep(_SECONDS_BETWEEN_REQUESTS)
                is_first_request = False

                url = CONCEPT_URL.format(cik=cik, taxonomy=taxonomy, tag=tag)
                response = self.session.get(url, headers=headers, timeout=30)
                if response.status_code == 404:
                    # Компанія не звітує цей концепт під цим тегом — не
                    # помилка адаптера, звичайна ситуація (напр. старий
                    # тег Revenues замінено на новий, або навпаки).
                    logger.info(
                        "SEC EDGAR %s: концепт %s/%s відсутній (404), пропущено",
                        self.ticker, taxonomy, tag,
                    )
                    continue
                response.raise_for_status()
                results.setdefault(suffix, []).append(response.json())

        if not results:
            total_variants = sum(len(v) for v in METRICS.values())
            raise ValueError(
                f"SEC EDGAR: жоден з {total_variants} варіантів концептів не "
                f"знайдено для {self.ticker} (CIK {cik}) — ймовірно, невірний "
                f"CIK чи тикер."
            )
        return results

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)
        records: list[NormalizedRecord] = []

        for suffix, payloads in raw_response.items():
            # Крок 1: фільтр форми й довжини періоду — по ВСІХ payload'ах
            # цього суфікса разом (кілька тегів-варіантів, див. METRICS).
            filtered: list[tuple[date, dict]] = []
            for payload in payloads:
                units = payload.get("units", {}) if isinstance(payload, dict) else {}
                entries = next(iter(units.values()), None)  # перший (єдиний очікуваний) unit
                if not entries:
                    continue

                for entry in entries:
                    form = entry.get("form")
                    if form not in _ALLOWED_FORMS:
                        continue

                    raw_end = entry.get("end")
                    raw_start = entry.get("start")
                    raw_val = entry.get("val")
                    if not raw_end or raw_val is None:
                        continue

                    try:
                        observed_at = date.fromisoformat(raw_end)
                    except ValueError:
                        continue

                    if raw_start:
                        # duration-концепт — відсіюємо кумулятивні періоди,
                        # лишаємо тільки одноквартальні (див. docstring).
                        try:
                            start_date = date.fromisoformat(raw_start)
                        except ValueError:
                            continue
                        period_days = (observed_at - start_date).days
                        if not (_MIN_QUARTER_DAYS <= period_days <= _MAX_QUARTER_DAYS):
                            continue

                    filtered.append((observed_at, entry))

            # Крок 2: дедуплікація по даті. SEC для того самого кварталу
            # часом дає два записи з РІЗНИМ val — типовий випадок: спліт
            # акцій, і пізніше подання перераховує EPS під нову к-сть
            # акцій за той самий історичний період (виявлено живим
            # тестом 2026-09-21 на aapl_eps_diluted та ін. — виглядало
            # як "ping-pong" ревізії 1.97→13.81→1.97...). Це не ревізія
            # факту, а перерахунок одиниць — беремо запис з найпізнішим
            # "filed" (найактуальніше подання), не обидва як конкурентів.
            by_date: dict[date, dict] = {}
            for observed_at, entry in filtered:
                existing = by_date.get(observed_at)
                if existing is None or entry.get("filed", "") > existing.get("filed", ""):
                    by_date[observed_at] = entry

            for observed_at, entry in by_date.items():
                raw_val = entry.get("val")
                try:
                    value = Decimal(str(raw_val))
                except InvalidOperation:
                    logger.warning(
                        "SEC EDGAR %s: не вдалось розпарсити %s за %s: %r",
                        self.ticker, suffix, observed_at, raw_val,
                    )
                    continue

                records.append(
                    NormalizedRecord(
                        source=self.source,
                        metric_id=f"{self.ticker.lower()}_{suffix}",
                        value=value,
                        observed_at=observed_at,
                        fetched_at=fetched_at,
                        revision=None,  # визначається шаром збереження, див. common/db.py
                        raw_payload=entry,
                    )
                )

        return records
