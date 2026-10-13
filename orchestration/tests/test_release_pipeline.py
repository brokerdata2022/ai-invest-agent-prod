"""
Тести релізного конвеєра (`orchestration/release_pipeline.py`).

Головне, що стережеться: гейт "чи є робота" мусить бути СИНХРОННИЙ із
фільтрами notify-скриптів. Якщо гейт ширший — він назавжди
залишається True й конвеєр ходить щохвилини марно (рівно те, що
сталось на першому живому прогоні: рядок `impact='low'` від 1 жовтня й
`linear_trend` від 27 вересня тримали гейт відкритим вічно).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from release_pipeline import STEPS, WORK_QUERY


def test_update_forecasts_runs_before_compare_expectations():
    """Єдиний порядок у конвеєрі, порушення якого НЕ самолікується:
    обидва кроки читають 'detected'-рядки, але власником переходу в
    'processed' є compare_releases.py. При зворотному порядку прогноз
    не оновиться НІКОЛИ."""
    names = [name for name, _ in STEPS]
    assert names.index("update_forecasts") < names.index("compare_expectations")


def test_gate_mirrors_impact_filter_of_expectations_notify():
    """`expectations_notify.py` надсилає лише high/medium. Без цього
    фільтра в гейті рядок із impact='low' тримає конвеєр відкритим
    вічно (живий стан: id=3 від 1 жовтня)."""
    assert "impact_level IN ('high', 'medium')" in WORK_QUERY


def test_gate_excludes_naive_and_legacy_forecast_methods():
    """`forecast_notify.py` не надсилає naive_* (сказало б "без змін")
    і виключає `linear_trend` (стара базова лінія). Живий стан: рядок
    id=1 від 27 вересня з method='linear_trend' висить ненадісланим
    назавжди."""
    assert "naive%" in WORK_QUERY
    assert "linear_trend" in WORK_QUERY


def test_gate_mirrors_asset_impacts_filter():
    """`release_impact_notify.py` надсилає лише синтези з непорожнім
    `asset_impacts`."""
    assert "jsonb_array_length(asset_impacts) > 0" in WORK_QUERY


def test_all_steps_point_at_existing_scripts():
    for name, script in STEPS:
        assert script.is_file(), f"{name}: немає файлу {script}"
