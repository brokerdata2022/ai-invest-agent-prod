"""
Економічний календар з датою+ЧАСОМ+impact+forecast — доповнення до
release_calendar.py (FRED дає тільки ДАТУ релізу, без часу й без
консенсус-прогнозу), і ЄДИНЕ джерело дати для eurozone_*/japan_*
показників (ECB/BOJ/e-Stat не мають перевіреного графіка релізів у
цьому проєкті).

Джерело: `https://nfs.faireconomy.media/ff_calendar_thisweek.json` —
неофіційна, широко використовувана редистрибуція календаря
ForexFactory. Без ключа. Поля: `title`/`country`/`date` (ISO8601 з
офсетом)/`impact` (High/Medium/Low/Holiday — уже розмічено)/`forecast`/
`previous`. Тільки поточний тиждень — немає "nextweek" чи історії.
BLS/investing.com розглядались і відкинуті (403, бот-захист) — деталі
й живі перевірки: `docs/decisions.md`, `docs/archive/decisions-full-2026-09-27.md`.

**Критичний нюанс:** `country` — обов'язковий фільтр, не опційний.
Однакові назви ("Unemployment Rate") повторюються для різних країн у
тому самому фіді — без фільтра `find_event()` міг би зловити чужу
країну. TITLE_PATTERNS тому `(country, patterns)`, не просто `patterns`.

**Підтвердження назв — частково:** лише `initial_jobless_claims`
("Unemployment Claims") підтверджено живим збігом; решта — зі сталої
термінології ForexFactory, не перевірено живо. Розбіжність → `None`,
найгірший наслідок — fallback (FRED-метрики) чи пропуск тижня
(eurozone_*/japan_*), ніколи не падає.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# metric_id → (country, підрядки титулу без урахування регістру).
# Метрики, де є схожа "Core"-версія під тим самим коренем назви (CPI/
# PCE/Retail Sales/HICP), явно виключаються нижче в _matches(), щоб
# headline-патерн не зловив "Core"-рядок помилково.
TITLE_PATTERNS: dict[str, tuple[str, list[str]]] = {
    # --- США (FRED-анкоровані, release_calendar.py дає дату) ---
    "initial_jobless_claims": ("USD", ["unemployment claims"]),  # live-підтверджено 2026-09-26
    "cpi": ("USD", ["cpi m/m"]),
    "core_cpi": ("USD", ["core cpi m/m"]),
    "pce_price_index": ("USD", ["pce price index m/m"]),
    "nonfarm_payrolls": ("USD", ["non-farm employment change"]),
    "unemployment_rate": ("USD", ["unemployment rate"]),
    "real_gdp": ("USD", ["gdp q/q"]),  # ловить Advance/Prelim/Final GDP q/q
    "retail_sales": ("USD", ["retail sales m/m"]),
    "housing_starts": ("USD", ["housing starts"]),
    # Рішення FOMC по ставці — ДОДАНО 2026-10-11. Доти в мапі були
    # рішення ECB ("deposit facility rate") і BOJ ("boj policy rate"),
    # а Fed — ні, тому `fed_funds_rate` не мав ЖОДНОЇ дати засідання в
    # release_log і прогнозувався "на завтра". Щоденна ефективна
    # ставка між засіданнями стоїть на місці — звідси й вердикт
    # "нічия" в бектесті: ми прогнозували не те, що змінюється.
    #
    # Фідбек користувача того дня: "ставка прогнозується після виходу
    # нових макроекономічних даних + новини + виступи банків та голів
    # комісій" — тобто ціль прогнозу це рішення НА ЗАСІДАННІ, і для
    # цього потрібна його дата.
    #
    # Патерн лише на сам рядок зі значенням ставки: "FOMC Statement" і
    # "FOMC Press Conference" у фіді окремі події без числа, а
    # "FOMC Member X Speaks" — виступи (вони потрібні як ВХІД
    # прогнозу, не як його дата).
    "fed_funds_rate": ("USD", ["federal funds rate"]),
    # mortgage_rate_30y (Freddie Mac survey) — FX-орієнтовані
    # календарі типово не трекають, немає патерна — завжди fallback.

    # --- Єврозона / Японія (без FRED-якоря — цей фід ЄДИНЕ джерело
    # дати, не тільки збагачення; докладніше refresh_calendar.py) ---
    "eurozone_hicp": ("EUR", ["cpi flash estimate y/y"]),
    "eurozone_deposit_rate": ("EUR", ["deposit facility rate"]),
    "eurozone_unemployment_rate": ("EUR", ["unemployment rate"]),
    "japan_policy_rate": ("JPY", ["boj policy rate", "monetary policy statement"]),
    "japan_cpi": ("JPY", ["national cpi y/y"]),
}

# metric_id, для яких існує "Core "-варіант тієї самої назви — щоб
# headline-патерн ("cpi m/m") не зловив "Core CPI m/m" помилково.
_EXCLUDE_CORE_VARIANT = {"cpi", "pce_price_index", "retail_sales", "eurozone_hicp"}


def fetch_calendar(session: Optional[requests.Session] = None) -> Any:
    sess = session or requests.Session()
    # User-Agent — без нього деякі мережі/CDN віддають 403 (те саме
    # застереження, що для BLS вище); для цього ендпоінта живо не
    # знадобилось, лишаємо про всяк випадок.
    response = sess.get(CALENDAR_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    return response.json()


def _matches(metric_id: str, event: dict) -> bool:
    pattern_entry = TITLE_PATTERNS.get(metric_id)
    if not pattern_entry:
        return False
    country, patterns = pattern_entry

    if event.get("country") != country:
        return False

    title_lower = event.get("title", "").lower()
    if not any(p in title_lower for p in patterns):
        return False
    if metric_id in _EXCLUDE_CORE_VARIANT and "core" in title_lower:
        return False
    return True


def find_event(metric_id: str, events: Any, on_date: date) -> Optional[dict]:
    """Знаходить подію календаря за metric_id (TITLE_PATTERNS, з
    фільтром по country) і КОНКРЕТНОЮ датою (за локальною датою офсету
    самої події, не UTC) — для FRED-анкорованих метрик, де дата вже
    відома з release_calendar.py і треба тільки збагачення.

    Повертає None, якщо немає шаблону чи збігу — виклик має
    ДЕГРАДУВАТИ (fallback у release_calendar.py), не падати.
    """
    if not isinstance(events, list):
        return None

    for event in events:
        if not _matches(metric_id, event):
            continue
        raw_date = event.get("date")
        try:
            event_dt = datetime.fromisoformat(raw_date)
        except (TypeError, ValueError):
            logger.warning("economic_calendar: не вдалось розпарсити дату події %r", event)
            continue
        if event_dt.date() == on_date:
            return event

    return None


def find_upcoming_event(
    metric_id: str,
    events: Any,
    today: Optional[date] = None,
    horizon_days: int = 7,
) -> Optional[dict]:
    """Знаходить НАЙБЛИЖЧУ ще не минулу подію за metric_id (з фільтром
    по country), у межах горизонту — БЕЗ конкретної дати-якоря
    (на відміну від find_event()). Для eurozone_*/japan_* показників,
    де цей фід — ЄДИНЕ джерело дати релізу, не тільки збагачення.

    Повертає None, якщо немає шаблону, збігу чи все поза горизонтом —
    виклик (refresh_calendar.py) просто пропускає цей тиждень для
    метрики, спробує знову наступного тижневого прогону."""
    if not isinstance(events, list):
        return None

    today = today or datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=horizon_days)

    candidates = []
    for event in events:
        if not _matches(metric_id, event):
            continue
        raw_date = event.get("date")
        try:
            event_dt = datetime.fromisoformat(raw_date)
        except (TypeError, ValueError):
            logger.warning("economic_calendar: не вдалось розпарсити дату події %r", event)
            continue
        if today <= event_dt.date() <= horizon:
            candidates.append((event_dt, event))

    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0])
    return candidates[0][1]
