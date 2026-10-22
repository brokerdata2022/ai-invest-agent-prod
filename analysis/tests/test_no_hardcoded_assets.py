"""
Тест-охоронець: у коді немає ПЕРЕЛІКІВ і властивостей активів списку
обраних — усе береться з `watchlist_assets` (рішення користувача
2026-10-22: "один список — усе від нього, раз і назавжди").

**Чому тест, а не правило в CLAUDE.md.** Живий кейс SKYD: тип активу,
категорії макровпливу, терміни пошуку, ланцюжки джерел, допуск звірки
жили хардкодом у семи місцях, кожне з поясненням "тимчасово" чи
"фолбек". Доданий через Telegram актив тихо працював на половину, і
ніщо не падало. Правило в документі цього не зупинило — тест зупинить:
він ламається на першому ж рядку `"xauusd"` у коді.

**Що ловить.** Будь-який рядковий літерал, що ДОСЛІВНО дорівнює
asset_id активу зі схеми (`db/schema.sql`), поза:
- тестами й `production/`;
- адаптерами (`*_adapter.py`): відповідність "наш metric_id → символ
  джерела" — їхня пряма робота (правило 2), а ЯКІ активи збирати,
  вирішує таблиця;
- ручними чек-листами в config (`ASSET_FACTORS`, `ASSET_FACTOR_GAPS`):
  це необовʼязкові перекриття знань про конкретний актив, які читаються
  через `.get(asset_id)` і нічого не перелічують;
- докстрінгами (рядок-вираз).

Новий виняток — лише з поясненням у `ALLOWED`.
"""

import ast
from pathlib import Path

from _schema_seed import seeded_asset_ids

REPO = Path(__file__).resolve().parents[2]

_SKIP_DIRS = {"tests", "production", "__pycache__", ".git", "logs", "docs", "db", "scripts"}

# Ручні знання про конкретний актив (необовʼязкові перекриття).
_CONFIG_OVERRIDES = {"ASSET_FACTORS", "ASSET_FACTOR_GAPS"}

# (файл відносно кореня, літерал) → чому це не перелік активів.
ALLOWED = {
    ("analysis/news_analysis/synthesize_market.py", "eurusd"):
        "metric_id FRED-серії DEXUSEU у макроконтексті синтезу ринку — "
        "показник, а не актив списку обраних",
}


def _python_files():
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if path.name.endswith("_adapter.py"):
            continue
        yield path, rel.as_posix()


def _override_ranges(tree: ast.Module) -> list[tuple[int, int]]:
    ranges = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        if _CONFIG_OVERRIDES & set(targets):
            ranges.append((node.lineno, node.end_lineno))
    return ranges


def find_hardcoded_assets() -> list[str]:
    asset_ids = seeded_asset_ids()
    found = []
    for path, rel in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        }
        overrides = _override_ranges(tree) if rel == "config.py" else []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if node.value not in asset_ids or id(node) in docstrings:
                continue
            if any(start <= node.lineno <= end for start, end in overrides):
                continue
            if (rel, node.value) in ALLOWED:
                continue
            found.append(f"{rel}:{node.lineno}: {node.value!r}")
    return found


def test_seed_parsing_sees_initial_assets():
    """Без цього охоронець міг би пройти просто тому, що не знайшов
    жодного asset_id у схемі."""
    assert {"xauusd", "btc", "eurusd", "coffee"} <= seeded_asset_ids()


def test_no_hardcoded_watchlist_assets_in_code():
    found = find_hardcoded_assets()
    assert not found, (
        "Перелік/властивість активу захардкоджено в коді — має братись із "
        "watchlist_assets (див. докстрінг цього тесту):\n" + "\n".join(found)
    )


def test_allowed_exceptions_are_still_needed():
    """Виняток, якого вже немає в коді, — мертвий запис, що колись
    прикриє новий хардкод у тому самому файлі."""
    for rel, literal in ALLOWED:
        source = (REPO / rel).read_text(encoding="utf-8")
        assert f'"{literal}"' in source or f"'{literal}'" in source, (rel, literal)
