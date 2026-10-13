from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from jobs import JOBS
from schedule import SCHEDULE, TIMEZONE


def test_schedule_and_jobs_names_match():
    assert set(SCHEDULE) == set(JOBS), "Кожна джоба в schedule.py має відповідник у jobs.py і навпаки"


def test_every_schedule_entry_has_trigger_and_why():
    for name, cfg in SCHEDULE.items():
        assert cfg.get("trigger"), f"{name}: відсутній trigger"
        assert cfg.get("why"), f"{name}: відсутнє пояснення 'why'"


def test_trigger_kwargs_are_valid_cron_fields():
    for name, cfg in SCHEDULE.items():
        CronTrigger(**cfg["trigger"])  # кидає виняток, якщо аргумент невалідний


def test_every_job_is_subprocess_or_callable():
    for name, spec in JOBS.items():
        assert ("subprocess" in spec) ^ ("callable" in spec), (
            f"{name}: рівно один із 'subprocess'/'callable', не обидва й не жоден"
        )


def test_release_pipeline_step_order_is_preserved():
    """ПЕРЕПИСАНО 2026-10-13. Раніше ланцюжок був СІМОМА окремими
    джобами на щохвилинному тригері, і порядок тримався на зсуві по
    секундах у розкладі — цей тест його й перевіряв. Тепер це ОДИН
    процес (`orchestration/release_pipeline.py`), тож порядок живе в
    коді, і стерегти треба саме його.

    Критично й незмінно: `update_forecasts` МАЄ йти перед
    `compare_expectations`. Обидва читають рядки `release_log` у
    статусі 'detected', але власником переходу в 'processed' є
    `compare_releases.py`. При зворотному порядку рядок стане
    'processed' раніше, ніж прогноз його побачить, і прогноз НЕ
    ОНОВИТЬСЯ ніколи — це не самолікується наступного тику, на
    відміну від решти пар."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from release_pipeline import STEPS

    names = [name for name, _ in STEPS]
    assert names.index("update_forecasts") < names.index("compare_expectations"), (
        "update_forecasts мусить іти ПЕРЕД compare_expectations — інакше "
        "'detected'-рядок стане 'processed' до оновлення прогнозу"
    )
    assert names.index("check_releases") == 0, "детекція релізу — перший крок"
    assert names.index("synthesize_expectations") < names.index("notify_expectations")
    assert names.index("synthesize_expectations") < names.index("notify_release_impact")


def test_release_pipeline_steps_point_at_existing_scripts():
    """Шлях, що не існує, провалився б лише в рантаймі о 3 ранку."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from release_pipeline import STEPS

    for name, script in STEPS:
        assert script.is_file(), f"{name}: немає файлу {script}"


# ============================================================
# Звіт НЕ МОЖЕ стояти перед джобою, що готує йому дані
# ============================================================
# Живий збій 2026-10-06 (фідбек користувача: "кава прийшла ціна в
# телеграм за 02.10"): `notify_watchlist` стояв о 5:20, а
# `watchlist_prices` — о 6:00. Звіт виходив ЗА 40 ХВИЛИН ДО збору,
# тобто фізично не міг показати свіжі дані, і щодня подавав учорашні
# як поточні. Жоден тест цього не ловив, бо кожен запис розкладу
# перевірявся ОКРЕМО.
#
# Пояси тут критичні: сесійні синтези живуть у часі свого ринку
# (Asia/Tokyo, Europe/London, America/New_York), а notify — у
# київському. Порівнювати "хвилини від полуночі" без перерахунку в
# один пояс означає знайти інверсію там, де її немає (саме так я й
# помилився при першій перевірці вручну). Тому обидва боки
# переводяться в UTC-інстанти.

# (хто готує дані, хто їх надсилає)
PRODUCER_NOTIFIER_PAIRS = [
    ("watchlist_prices", "notify_watchlist"),
    ("screening_composite_score", "notify_screening"),
    ("crypto_screening_daily", "notify_crypto_screening"),
    ("trading_list", "notify_trading_list"),
    ("fundamental_analysis", "notify_fundamental"),
    ("calendar_outlook", "notify_calendar_outlook"),
    ("discover_candidates", "notify_candidates"),
    ("news_consolidate", "news_notify"),
]

# Дві дати: літній і зимовий час — інверсія може зʼявитись лише в
# одному з режимів, якщо пояси джоб різні (той самий клас помилки, що
# DST-фікс сесійних синтезів 2026-10-04).
_DST_REFERENCE_DATES = (date(2026, 7, 1), date(2026, 1, 15))


def _fire_times_utc(name: str, reference: date) -> list[datetime]:
    """Усі моменти запуску джоби за добу `reference`, у UTC."""
    cfg = SCHEDULE[name]
    trigger = cfg["trigger"]
    zone = ZoneInfo(cfg.get("timezone", TIMEZONE))

    def field(key, default):
        raw = trigger.get(key, default)
        if raw in ("*", None):
            return list(range(24)) if key == "hour" else [0]
        return [int(x) for x in str(raw).split(",")]

    return sorted(
        datetime(reference.year, reference.month, reference.day, h, m, tzinfo=zone).astimezone(
            ZoneInfo("UTC")
        )
        for h in field("hour", 0)
        for m in field("minute", 0)
    )


@pytest.mark.parametrize("producer,notifier", PRODUCER_NOTIFIER_PAIRS)
@pytest.mark.parametrize("reference", _DST_REFERENCE_DATES)
def test_notifier_runs_after_its_producer(producer, notifier, reference):
    """КОЖЕН запуск звіту мусить мати запуск джоби-постачальника
    ПЕРЕД ним у ту саму добу. Інакше звіт подає старі дані як
    поточні — і це не видно ні в логах, ні в самому звіті."""
    producer_times = _fire_times_utc(producer, reference)
    notifier_times = _fire_times_utc(notifier, reference)

    for sent_at in notifier_times:
        assert any(made_at < sent_at for made_at in producer_times), (
            f"{notifier} о {sent_at:%H:%M} UTC ({reference}) виходить ДО того, як "
            f"{producer} приготував дані (запуски: "
            f"{[f'{t:%H:%M}' for t in producer_times]} UTC)"
        )
