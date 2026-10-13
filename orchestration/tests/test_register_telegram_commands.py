"""
Тести build_commands_payload() — чиста логіка (словники → список для
Telegram setMyCommands), без мережі."""

from command_descriptions import INFO_JOB_NAMES, JOB_DESCRIPTIONS, SPECIAL_COMMAND_DESCRIPTIONS
from register_telegram_commands import build_commands_payload


def test_build_commands_payload_includes_only_info_jobs_and_special_commands():
    # 2026-10-03: технічні джоби (збір/аналіз без доставки) більше не
    # потрапляють у меню — лише INFO_JOB_NAMES + watchlist-команди.
    payload = build_commands_payload()
    names = {entry["command"] for entry in payload}

    assert names == INFO_JOB_NAMES | set(SPECIAL_COMMAND_DESCRIPTIONS)
    assert len(payload) == len(names)  # жодного дубліката


def test_build_commands_payload_excludes_technical_jobs():
    payload = build_commands_payload()
    names = {entry["command"] for entry in payload}
    assert "check_releases" not in names
    assert "news_collect_watchlist" not in names
    assert "discover_candidates" not in names


def test_build_commands_payload_is_sorted_by_name():
    payload = build_commands_payload()
    names = [entry["command"] for entry in payload]
    assert names == sorted(names)


def test_build_commands_payload_matches_description_text():
    payload = build_commands_payload()
    by_name = {entry["command"]: entry["description"] for entry in payload}

    assert by_name["notify_screening"] == JOB_DESCRIPTIONS["notify_screening"]
    assert by_name["watchlist_add"] == SPECIAL_COMMAND_DESCRIPTIONS["watchlist_add"]
