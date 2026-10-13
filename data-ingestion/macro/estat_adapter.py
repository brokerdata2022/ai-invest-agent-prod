"""
Адаптер e-Stat (政府統計の総合窓口) API v3.0 — офіційне першоджерело
для CPI Японії. https://www.e-stat.go.jp/api/

Четвертий адаптер проєкту. На відміну від FRED/ECB/BOJ, тут ключ
(`appId`) обов'язковий — безкоштовна реєстрація на e-stat.go.jp,
до 3 appId на акаунт. Передається через ESTAT_APP_ID у .env, за тим
самим патерном, що й FRED_API_KEY.

СТАТИСТИЧНА ТАБЛИЦЯ: 2025年基準消費者物価指数 (CPI, база 2025=100,
чинна з 21.08.2026), statsDataId=0004052037. Старий statsDataId
0003427113 (база 2020=100) продовжує оновлюватись лише до грудня
2026 — після цього чинним лишається тільки новий (див.
docs/decisions.md, запис про базову ревізію Японія CPI, той самий
сценарій, що й з ECB ICP→HICP).

ЧОМУ ДВА МЕРЕЖЕВІ ВИКЛИКИ В fetch(): на відміну від FRED/ECB/BOJ, де
код серії відомий заздалегідь, e-Stat ідентифікує зріз даних кодами
класифікації, які видаються разом із самими даними як метадані
(CLASS_INF), а не документовані окремо у зручному вигляді. Тому:
  1) невеликий запит (limit=1, metaGetFlg=Y) — тільки щоб отримати
     CLASS_INF і по ньому резолвити код кожного виміру таблиці (area,
     cat01, tab, ...) окрім time;
  2) основний запит уже з конкретними cdArea/cdCat01/cdTab/... —
     компактна відповідь тільки з потрібним рядом.

ВИПРАВЛЕНО 2026-09-13 (живі дані): перша версія резолвила тільки
area/cat01 і ігнорувала вимір `tab`. Виявилось, що ця таблиця CPI
публікує кілька рядків на кожен місяць під різними кодами `tab` —
сам індекс (~101.5) окремо від %-змін м/м і р/р (~0.5, ~1.9) — як
ОКРЕМІ значення в тій самій таблиці, не варіанти того самого числа.
Без фільтрації за tab усі три потрапляли під один metric_id і
перезаписували одне одного як фальшиві "ревізії" в raw_observations.
Деталі — docs/decisions.md. Тепер `_resolve_dimensions()` резолвить
УСІ виміри з кількома можливими значеннями (не тільки area/cat01),
використовуючи `_DIMENSION_PREFERENCES` для людинозрозумілого вибору
потрібного варіанту; невідомий вимір з кількома значеннями, для якого
немає запису в `_DIMENSION_PREFERENCES`, явно кидає ValueError
замість мовчазного змішування даних.
"""

import logging
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import requests

from common.adapter import BaseAdapter, NormalizedRecord

logger = logging.getLogger(__name__)

ESTAT_API_URL = "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData"

# metric_id → statsDataId. Для кожного нового Японія-показника з
# e-Stat (напр. Unemployment Rate) достатньо додати новий рядок тут —
# резолюція area/cat01 і парсинг залишаються спільні.
METRICS: dict[str, str] = {
    "japan_cpi": "0004052037",  # 2025年基準消費者物価指数, 総合, 全国
}

# Назви, за якими шукаємо потрібний зріз у метаданих CLASS_INF —
# людинозрозумілі рядки стабільніші за самі коди, які можуть
# відрізнятись між таблицями/базовими роками.
_NATIONAL_AREA_NAME = "全国"
_ALL_ITEMS_CAT_NAME = "総合"
_INDEX_TAB_NAME = "指数"  # сам рівень індексу, а не %-зміни (前年同月比/前月比)

# Для кожного виміру CLASS_INF (крім "time", якого не резолвимо тут),
# у якого таблиця пропонує БІЛЬШЕ ОДНОГО значення, потрібне явно
# вказане людинозрозуміле ім'я потрібного варіанту — інакше e-Stat
# поверне ВСІ варіанти під одним metric_id, і вони перезапишуть одне
# одного як фальшиві "ревізії" (саме так виявили баг 2026-09-13).
# Якщо з'явиться новий вимір із кількома значеннями, якого тут нема —
# _resolve_dimensions() свідомо кине ValueError, а не вгадуватиме.
_DIMENSION_PREFERENCES: dict[str, str] = {
    "area": _NATIONAL_AREA_NAME,
    "cat01": _ALL_ITEMS_CAT_NAME,
    "tab": _INDEX_TAB_NAME,
}

_TIME_PATTERNS = (
    re.compile(r"(\d{4})年(\d{1,2})月"),  # "2026年07月" → місячний
    re.compile(r"^(\d{4})(\d{2})$"),       # "202607" (про всяк випадок)
)


def _parse_estat_time(name: str) -> Optional[date]:
    for pattern in _TIME_PATTERNS:
        match = pattern.search(name)
        if match:
            year, month = int(match.group(1)), int(match.group(2))
            return date(year, month, 1)
    return None


def _find_class_code(class_inf: Any, class_id: str, name_needle: str) -> Optional[str]:
    """Шукає в CLASS_INF.CLASS_OBJ об'єкт з @id == class_id, і в
    ньому — CLASS-запис, чия @name точно дорівнює name_needle
    (пріоритет) або містить його (фолбек, якщо точного співпадіння
    немає — деякі таблиці додають примітки в дужках до назви)."""
    class_objs = class_inf.get("CLASS_OBJ") if isinstance(class_inf, dict) else None
    if class_objs is None:
        return None
    if isinstance(class_objs, dict):
        class_objs = [class_objs]

    for obj in class_objs:
        if obj.get("@id") != class_id:
            continue
        entries = obj.get("CLASS")
        if isinstance(entries, dict):
            entries = [entries]
        entries = entries or []

        for entry in entries:
            if entry.get("@name") == name_needle:
                return entry.get("@code")
        for entry in entries:
            if name_needle in (entry.get("@name") or ""):
                return entry.get("@code")
    return None


def _get_class_name(class_inf: Any, class_id: str, code: str) -> Optional[str]:
    class_objs = class_inf.get("CLASS_OBJ") if isinstance(class_inf, dict) else None
    if isinstance(class_objs, dict):
        class_objs = [class_objs]
    for obj in class_objs or []:
        if obj.get("@id") != class_id:
            continue
        entries = obj.get("CLASS")
        if isinstance(entries, dict):
            entries = [entries]
        for entry in entries or []:
            if entry.get("@code") == code:
                return entry.get("@name")
    return None


class EstatAdapter(BaseAdapter):
    source = "estat"

    def __init__(self, api_key: str, metric_id: str, session: Optional[requests.Session] = None):
        # Параметр названо api_key (не app_id), щоб збігатись зі
        # спільним патерном інстанціації в run_collect.py/collect_all.py
        # (той самий, що й у FredAdapter) — по суті це appId e-Stat.
        if not api_key:
            raise ValueError(
                "ESTAT_APP_ID не задано. Зареєструйте безкоштовний appId на "
                "https://www.e-stat.go.jp (My Page → API機能 → "
                "アプリケーションIDの発行) і додайте його в .env."
            )
        if metric_id not in METRICS:
            raise ValueError(
                f"Невідомий metric_id для e-Stat: {metric_id!r}. "
                f"Доступні: {sorted(METRICS)}"
            )
        self.app_id = api_key
        self.metric_id = metric_id
        self.stats_data_id = METRICS[metric_id]
        self.session = session or requests.Session()

    def _resolve_dimensions(self) -> dict[str, str]:
        """Крок 1: невеликий запит лише за метаданими; резолвить код
        КОЖНОГО виміру класифікації цієї таблиці, крім "time".

        Вимір з рівно одним можливим значенням — беремо його без
        додаткових питань (немає неоднозначності). Вимір з кількома
        значеннями — резолвимо через `_DIMENSION_PREFERENCES`; якщо
        такого виміру там нема, кидаємо ValueError з переліком
        доступних назв замість мовчазного змішування різних величин
        під одним metric_id (див. докстрінг модуля, випадок 2026-09-13
        з виміром `tab`)."""
        params = {
            "appId": self.app_id,
            "statsDataId": self.stats_data_id,
            "metaGetFlg": "Y",
            "cntGetFlg": "N",
            "limit": 1,
        }
        response = self.session.get(ESTAT_API_URL, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()

        stats_data = payload.get("GET_STATS_DATA", {}).get("STATISTICAL_DATA", {})
        class_inf = stats_data.get("CLASS_INF", {})
        class_objs = class_inf.get("CLASS_OBJ") if isinstance(class_inf, dict) else None
        if isinstance(class_objs, dict):
            class_objs = [class_objs]
        class_objs = class_objs or []

        resolved: dict[str, str] = {}
        for obj in class_objs:
            dim_id = obj.get("@id")
            if not dim_id or dim_id == "time":
                continue

            entries = obj.get("CLASS")
            if isinstance(entries, dict):
                entries = [entries]
            entries = entries or []
            if not entries:
                continue

            if len(entries) == 1:
                # Єдине можливе значення — неоднозначності нема.
                resolved[dim_id] = entries[0].get("@code")
                continue

            preferred_name = _DIMENSION_PREFERENCES.get(dim_id)
            if preferred_name is None:
                available = [e.get("@name") for e in entries]
                raise ValueError(
                    f"Таблиця e-Stat statsDataId={self.stats_data_id} має "
                    f"кілька значень виміру '{dim_id}' {available!r}, але "
                    f"немає визначеного пріоритету в _DIMENSION_PREFERENCES "
                    f"(estat_adapter.py). Без явного вибору e-Stat поверне "
                    f"ВСІ варіанти під одним metric_id і вони переплутаються "
                    f"між собою. Додайте "
                    f"_DIMENSION_PREFERENCES['{dim_id}'] = '<точна назва "
                    f"потрібного варіанту з переліку вище>' і повторіть."
                )

            code = _find_class_code(class_inf, dim_id, preferred_name)
            if code is None:
                available = [e.get("@name") for e in entries]
                raise ValueError(
                    f"Не вдалось знайти код '{preferred_name}' у вимірі "
                    f"'{dim_id}' для statsDataId={self.stats_data_id} "
                    f"(доступні назви: {available!r})."
                )
            resolved[dim_id] = code

        missing_required = [d for d in ("area", "cat01") if d not in resolved]
        if missing_required:
            raise ValueError(
                f"Очікувані виміри {missing_required!r} відсутні у "
                f"метаданих e-Stat для statsDataId={self.stats_data_id} "
                f"(знайдено виміри: {sorted(resolved)!r}). Структура "
                f"класифікації цієї таблиці могла відрізнятись від "
                f"очікуваної — перевірте вручну через getMetaInfo."
            )
        return resolved

    def fetch(
        self,
        limit: Optional[int] = None,
        observation_start: Optional[str] = None,
        observation_end: Optional[str] = None,
    ) -> Any:
        dimensions = self._resolve_dimensions()

        params: dict[str, Any] = {
            "appId": self.app_id,
            "statsDataId": self.stats_data_id,
            "metaGetFlg": "Y",  # лишаємо Y — normalize() використовує CLASS_INF для дат
            "cntGetFlg": "N",
        }
        for dim_id, code in dimensions.items():
            # e-Stat param naming: cd<Dim> у CamelCase, напр. area→cdArea,
            # cat01→cdCat01, tab→cdTab.
            params[f"cd{dim_id[0].upper()}{dim_id[1:]}"] = code
        # e-Stat не має limit "останні N" так само зручно, як FRED
        # (limit тут — "перші N рядків результату", не "останні") —
        # тому свідомо не передаємо limit сюди; фільтрація за limit
        # відбувається в normalize() після отримання повного ряду.

        response = self.session.get(ESTAT_API_URL, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def normalize(self, raw_response: Any, limit: Optional[int] = None) -> list[NormalizedRecord]:
        fetched_at = datetime.now(timezone.utc)

        stats_data = raw_response.get("GET_STATS_DATA", {}).get("STATISTICAL_DATA", {})
        class_inf = stats_data.get("CLASS_INF", {})
        data_inf = stats_data.get("DATA_INF", {})
        values = data_inf.get("VALUE", [])
        if isinstance(values, dict):
            values = [values]

        if not values:
            logger.warning(
                "Порожній DATA_INF.VALUE у відповіді e-Stat для %s "
                "(statsDataId=%s) — перевірте параметри запиту.",
                self.metric_id, self.stats_data_id,
            )
            return []

        # dict, а не list: захист від дублікатів на ту саму дату. Якщо
        # _resolve_dimensions() колись пропустить якийсь вимір із
        # кількома значеннями (напр. новий tab-код, доданий e-Stat
        # пізніше), фільтрація на рівні запиту не спрацює — і без цієї
        # перевірки різні величини (індекс/%-зміна) знову тихо
        # перезаписували б одна одну як фальшиві "ревізії", як це вже
        # сталось 2026-09-13 (див. докстрінг модуля).
        by_date: dict[date, NormalizedRecord] = {}
        conflicts: dict[date, list[Decimal]] = {}

        for entry in values:
            raw_value = entry.get("$")
            if raw_value in (None, "", "-", "..."):
                continue

            try:
                value = Decimal(str(raw_value))
            except InvalidOperation:
                logger.warning(
                    "Не вдалось розпарсити значення e-Stat %s: %r",
                    self.metric_id, raw_value,
                )
                continue

            time_code = entry.get("@time")
            time_name = _get_class_name(class_inf, "time", time_code) or str(time_code)
            observed_at = _parse_estat_time(time_name)
            if observed_at is None:
                logger.warning(
                    "Не вдалось розпарсити період e-Stat %r (код %r) для %s",
                    time_name, time_code, self.metric_id,
                )
                continue

            existing = by_date.get(observed_at)
            if existing is not None and existing.value != value:
                conflicts.setdefault(observed_at, [existing.value]).append(value)
                continue  # залишаємо перше значення, друге ігноруємо

            by_date[observed_at] = NormalizedRecord(
                source=self.source,
                metric_id=self.metric_id,
                value=value,
                observed_at=observed_at,
                fetched_at=fetched_at,
                revision=None,
                raw_payload=dict(entry),
            )

        if conflicts:
            logger.warning(
                "e-Stat %s: за одну дату знайдено кілька різних значень "
                "(%s) — залишено перше, решту проігноровано. Це означає, "
                "що _resolve_dimensions() недостатньо звузила запит; "
                "перевірте виміри статистичної таблиці вручну.",
                self.metric_id, conflicts,
            )

        records = sorted(by_date.values(), key=lambda r: r.observed_at)
        if limit:
            records = records[-limit:]
        return records

    def collect(self, **kwargs) -> list[NormalizedRecord]:
        limit = kwargs.pop("limit", None)
        raw = self.fetch(**kwargs)
        return self.normalize(raw, limit=limit)
