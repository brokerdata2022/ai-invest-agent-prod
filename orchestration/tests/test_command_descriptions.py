"""
Паритет command_descriptions.py:JOB_DESCRIPTIONS ↔ jobs.py:JOBS (той
самий принцип, що test_schedule.py:test_schedule_and_jobs_names_match)
+ обмеження Telegram setMyCommands на опис (1-256 символів, без
переносу рядка).
"""

from command_descriptions import INFO_JOB_NAMES, JOB_DESCRIPTIONS, SPECIAL_COMMAND_DESCRIPTIONS
from jobs import JOBS
from telegram_commands import WATCHLIST_COMMANDS


def test_every_job_has_a_description():
    assert set(JOB_DESCRIPTIONS) == set(JOBS), "Кожна джоба має опис і навпаки"


def test_every_watchlist_command_has_a_description():
    assert set(SPECIAL_COMMAND_DESCRIPTIONS) == set(WATCHLIST_COMMANDS)


def test_watchlist_commands_do_not_collide_with_job_names():
    assert set(SPECIAL_COMMAND_DESCRIPTIONS).isdisjoint(set(JOB_DESCRIPTIONS))


def test_info_job_names_are_a_subset_of_known_jobs():
    assert INFO_JOB_NAMES.issubset(set(JOBS))


def test_info_job_names_only_contain_delivery_jobs():
    # Критерій INFO_JOB_NAMES (docs/decisions.md 2026-10-03): джоба
    # сама надсилає інформацію користувачу — notify_*/daily_digest.
    #
    # `release_pipeline` доданий до винятків 2026-10-13: три окремі
    # notify-джоби (`notify_expectations`/`notify_forecasts`/
    # `notify_release_impact`) злиті в нього, тож ВІН тепер і є тим,
    # хто доставляє ці три звіти. Назва не починається з notify_, бо
    # конвеєр робить і збір, і аналіз — але для користувача це
    # залишається командою "дай мені релізні звіти".
    DELIVERY_EXCEPTIONS = ("daily_digest", "news_notify", "release_pipeline")
    for name in INFO_JOB_NAMES:
        assert name in DELIVERY_EXCEPTIONS or name.startswith("notify_"), (
            f"{name}: не схоже на delivery-джобу "
            f"(notify_* або один із {DELIVERY_EXCEPTIONS})"
        )


def test_descriptions_fit_telegram_limits():
    for name, description in {**JOB_DESCRIPTIONS, **SPECIAL_COMMAND_DESCRIPTIONS}.items():
        assert 1 <= len(description) <= 256, f"{name}: опис поза межами 1-256 символів"
        assert "\n" not in description, f"{name}: опис не повинен містити перенос рядка"
