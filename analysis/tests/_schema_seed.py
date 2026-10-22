"""
Початкові значення `watchlist_assets` прямо з `db/schema.sql` — для
тестів, яким потрібні справжні терміни/класи активів.

Навіщо читати схему, а не тримати копію в тесті (2026-10-22): терміни
пошуку переїхали з `config.py` у таблицю, і копія в тесті перевіряла б
сама себе. Так тест перевіряє рівно те, що ляже в БД після
`apply_schema.py`.
"""

import re
from pathlib import Path

SCHEMA = Path(__file__).resolve().parents[2] / "db" / "schema.sql"

_QUOTED = r"'(?:[^']|'')*'"
_UPDATE = re.compile(r"UPDATE watchlist_assets\s+SET([^;]*?)WHERE asset_id = '(\w+)'")
_INSERT = re.compile(r"INSERT INTO watchlist_assets \([^)]*\) VALUES(.*?)ON CONFLICT", re.DOTALL)


def _array(block: str, column: str) -> tuple[str, ...] | None:
    match = re.search(rf"{column} = ARRAY\[((?:\s*{_QUOTED}\s*,?)+)\]", block)
    if match is None:
        return None
    return tuple(item[1:-1].replace("''", "'") for item in re.findall(_QUOTED, match.group(1)))


def _scalar(block: str, column: str) -> str | None:
    match = re.search(rf"\b{column} = '((?:[^']|'')*)'", block)
    return match.group(1) if match else None


def seeded_asset_ids() -> set[str]:
    """asset_id усіх рядків, що схема вставляє або оновлює поіменно."""
    sql = SCHEMA.read_text(encoding="utf-8")
    ids = set()
    for values in _INSERT.findall(sql):
        ids.update(re.findall(r"\(\s*'(\w+)'", values))
    ids.update(asset_id for _block, asset_id in _UPDATE.findall(sql))
    return ids


def seeded_profiles() -> dict[str, dict]:
    """{asset_id: {asset_class, commodity_group, news_patterns, ask_patterns}}
    з UPDATE-блоків профілю (2026-10-22)."""
    sql = SCHEMA.read_text(encoding="utf-8")
    out: dict[str, dict] = {}
    for block, asset_id in _UPDATE.findall(sql):
        asset_class = _scalar(block, "asset_class")
        if asset_class is None:
            continue
        out[asset_id] = {
            "asset_id": asset_id,
            "asset_class": asset_class,
            "commodity_group": _scalar(block, "commodity_group"),
            "news_patterns": _array(block, "news_patterns"),
            "ask_patterns": _array(block, "ask_patterns"),
        }
    return out


def seeded_patterns(column: str) -> dict[str, tuple[str, ...]]:
    return {
        asset_id: profile[column]
        for asset_id, profile in seeded_profiles().items()
        if profile[column]
    }
