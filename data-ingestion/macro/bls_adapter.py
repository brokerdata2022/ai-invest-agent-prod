"""
BLS (Bureau of Labor Statistics) public API — ДРУГЕ незалежне джерело
для макропоказників США, щоб їх можна було ЗВІРЯТИ.

**Чому це з'явилось (2026-10-13).** Принцип користувача 2026-10-12:
"корекні дані це онлайн пошук і звірка на декількох джерелах". Для
watchlist-цін звірка вже працює (`common/consensus.py` +
`quotes/yahoo_chart_adapter.py`) і одразу зловила реальну проблему —
FRED давав Brent 135.51 проти справжніх 102.25. Але 19
МАКРОпоказників лишались на FRED БЕЗ ЖОДНОЇ звірки, і після кейсу з
Brent "там, напевно, усе гаразд" — це вже припущення, не знання.

**Чому саме BLS, а не ще один агрегатор.** FRED — це ДЗЕРКАЛО: він
сам передруковує дані BLS/BEA/Census. BLS — першоджерело тих самих
рядів. Тобто звірка тут не "два агрегатори погоджуються", а "дзеркало
збігається з оригіналом" — найсильніша форма перевірки, яка взагалі
можлива, і вона ловить саме той клас помилок, що був у Brent:
зіпсований/підмінений ряд на боці дзеркала.

**Живий замір ПЕРЕД кодом** (data-ingestion/CLAUDE.md). Один запит
без ключа, HTTP 200:

| показник | FRED (наша БД) | BLS | збіг |
|---|---|---|---|
| `cpi` 2026-08 | 334.131 | 334.131 | точний |
| `unemployment_rate` 2026-09 | 4.2 | 4.2 | точний |
| `nonfarm_payrolls` 2026-09 | 159044 | 159044 | точний |

**Пастка, на яку я наступив під час перевірки, і чому вона тут
задокументована:** спершу взяв `CUUR0000SA0` і отримав 334.980 проти
наших 334.131 — розбіжність 0.25%, яка виглядала як справжня
знахідка. Насправді `CUUR` — ряд БЕЗ сезонного згладжування, а
FRED `CPIAUCSL` — ЗІ згладжуванням; правильна пара — `CUSR0000SA0`,
і вона дає точний збіг. Тобто для звірки критично важливо брати ТОЙ
САМИЙ різновид ряду: інакше шар консенсусу генерував би постійні
хибні тривоги й знецінився б так само, як прапорець "нестабільні
ревізії" до його виправлення.

**Покриття — 4 показники з 19**, і це чесна межа: BLS відповідає за
ціни й ринок праці, а `real_gdp`/`pce_price_index` — це BEA,
`retail_sales`/`housing_starts` — Census, `initial_jobless_claims` —
DOL. Решта лишається без звірки й це задокументовано, а не замовчано.
Зате ці 4 — найцінніші: саме на `core_cpi` (+72%), `cpi` (+43%) і
`nonfarm_payrolls` (+44%) ШІ-прогноз дає найбільшу перевагу за
бектестом, тож хибне значення тут коштує найдорожче.

**Ліміт:** без ключа BLS дає 25 запитів/добу і до 25 серій на запит.
Усі наші 4 серії беруться ОДНИМ запитом, тож ліміт не проблема
(джоба звірки ходить раз на добу).
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

API_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"

# internal metric_id → BLS series id.
#
# ⚠️ УСІ ряди — СЕЗОННО ЗГЛАДЖЕНІ (`CUSR`/`LNS`/`CES`), бо саме такі
# ряди бере наш `macro/fred_adapter.py` (CPIAUCSL/CPILFESL/UNRATE/
# PAYEMS). Незгладжений аналог (`CUUR...`) дає інше число на тих
# самих даних — див. докстрінг модуля, це перевірено живо.
METRICS: dict[str, str] = {
    "cpi": "CUSR0000SA0",
    "core_cpi": "CUSR0000SA0L1E",
    "unemployment_rate": "LNS14000000",
    "nonfarm_payrolls": "CES0000000001",
}

# BLS віддає період як M01..M12 (місяць), Q01..Q04 (квартал), A01 (рік).
_MONTH_PERIODS = {f"M{i:02d}": i for i in range(1, 13)}


class BlsAdapter(BaseAdapter):
    source = "bls"

    def __init__(self, metric_id: str, api_key: Optional[str] = None,
                 session: Optional[requests.Session] = None):
        if metric_id not in METRICS:
            raise ValueError(
                f"Немає серії BLS для metric_id {metric_id!r}. "
                f"Доступні: {sorted(METRICS)}"
            )
        self.metric_id = metric_id
        self.series_id = METRICS[metric_id]
        # Ключ НЕ обов'язковий: публічний доступ (v2 без registrationkey)
        # дає 25 запитів/добу, чого з запасом досить для добової звірки.
        # Якщо ключ є в .env — використовуємо, він підіймає ліміт до 500.
        self._api_key = api_key
        self._session = session or requests.Session()

    def fetch(self, limit: Optional[int] = None, **_ignored) -> Any:
        """`limit` тут означає "скільки РОКІВ історії", а не точок:
        BLS приймає діапазон років, не кількість спостережень.

        Дефолт — поточний і минулий рік: для звірки потрібне останнє
        значення й трохи контексту, а не вся історія (її вже має FRED).
        """
        current_year = date.today().year
        years_back = 1 if not limit else max(1, min(limit // 12, 19))
        payload = {
            "seriesid": [self.series_id],
            "startyear": str(current_year - years_back),
            "endyear": str(current_year),
        }
        if self._api_key:
            payload["registrationkey"] = self._api_key

        response = self._session.post(
            API_URL, json=payload,
            headers={"Content-Type": "application/json"}, timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)

        status = (raw_response or {}).get("status")
        if status != "REQUEST_SUCCEEDED":
            # BLS повертає HTTP 200 навіть на помилку, а причину кладе
            # в `status`/`message` — рівно той клас пастки, що мʼякий
            # троттл GDELT (200 з порожнім тілом читався як "немає
            # новин" і місяць ховав поломку). Тому падаємо голосно.
            raise ValueError(
                f"BLS {self.series_id}: запит не вдався, status={status!r}, "
                f"message={(raw_response or {}).get('message')!r}"
            )

        series_list = raw_response.get("Results", {}).get("series", [])
        if not series_list:
            raise ValueError(f"BLS {self.series_id}: у відповіді немає жодної серії")

        records: list[NormalizedRecord] = []
        skipped = 0
        for point in series_list[0].get("data", []):
            month = _MONTH_PERIODS.get(point.get("period", ""))
            if month is None:
                # M13 — річне середнє, Q0x — квартал. Для звірки з
                # МІСЯЧНИМ рядом FRED вони не пара, тож пропускаємо.
                skipped += 1
                continue
            try:
                value = Decimal(str(point["value"]).replace(",", ""))
                observed_at = date(int(point["year"]), month, 1)
            except (KeyError, ValueError, InvalidOperation):
                skipped += 1
                continue
            records.append(
                NormalizedRecord(
                    source=self.source,
                    metric_id=self.metric_id,
                    value=value,
                    observed_at=observed_at,
                    fetched_at=fetched_at,
                    raw_payload={"series_id": self.series_id, "period": point.get("period")},
                )
            )

        if skipped:
            logger.info(
                "%s (%s): пропущено %d немісячних точок (річні/квартальні зведення)",
                self.metric_id, self.series_id, skipped,
            )
        if not records:
            raise ValueError(
                f"BLS {self.series_id}: жодної придатної місячної точки у відповіді"
            )
        return records


def fetch_all(api_key: Optional[str] = None) -> dict[str, list[NormalizedRecord]]:
    """Усі 4 показники ОДНИМ запитом — щоб не витрачати добовий ліміт
    (25 запитів без ключа) на чотири окремі виклики.

    Повертає {metric_id: записи}. Показник, який BLS не віддав, просто
    відсутній у результаті — звірка далі побачить "одне джерело" і
    скаже `insufficient`, а не вигадає згоду."""
    payload = {
        "seriesid": list(METRICS.values()),
        "startyear": str(date.today().year - 1),
        "endyear": str(date.today().year),
    }
    if api_key:
        payload["registrationkey"] = api_key

    response = requests.post(
        API_URL, json=payload,
        headers={"Content-Type": "application/json"}, timeout=30,
    )
    response.raise_for_status()
    body = response.json()

    if body.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError(
            f"BLS bulk: запит не вдався, status={body.get('status')!r}, "
            f"message={body.get('message')!r}"
        )

    by_series = {m: mid for mid, m in METRICS.items()}
    out: dict[str, list[NormalizedRecord]] = {}
    for series in body.get("Results", {}).get("series", []):
        metric_id = by_series.get(series.get("seriesID"))
        if metric_id is None:
            continue
        adapter = BlsAdapter(metric_id)
        # Переюзовуємо той самий normalize, обернувши серію у форму
        # одиночної відповіді — щоб логіка розбору була В ОДНОМУ місці.
        out[metric_id] = adapter.normalize(
            {"status": "REQUEST_SUCCEEDED", "Results": {"series": [series]}}
        )
    return out
