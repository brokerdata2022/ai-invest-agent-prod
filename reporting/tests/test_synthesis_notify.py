from datetime import date
from decimal import Decimal

from synthesis_notify import format_synthesis_message


def test_format_synthesis_message_known_row():
    row = {
        "asset_id": "xauusd",
        "cluster_count": 3,
        "net_lean": 2,
        "price_pct_change": Decimal("5.00"),
        "price_start_date": date(2026, 9, 20),
        "price_end_date": date(2026, 9, 27),
        "direction": "up",
        "confidence": 0.75,
        "summary": "Зростання ціни узгоджується з новинним сигналом.",
        "confirmation_factors": "Перевірити, чи триматимуться ставки високими далі.",
    }
    text = format_synthesis_message(row)
    assert "xauusd" in text
    assert "+5.00%" in text
    assert "2026-09-20" in text and "2026-09-27" in text
    assert "+2" in text
    assert "3 історій" in text
    assert "Зростання ціни узгоджується" in text
    assert "Перевірити: Перевірити, чи триматимуться" in text


def test_format_synthesis_message_unknown_direction_falls_back_to_question_emoji():
    row = {
        "asset_id": "wti_crude",
        "cluster_count": 1,
        "net_lean": 0,
        "price_pct_change": Decimal("-1.50"),
        "price_start_date": date(2026, 9, 20),
        "price_end_date": date(2026, 9, 27),
        "direction": "something_new",
        "confidence": 0.5,
        "summary": "Немає чіткого сигналу.",
        "confirmation_factors": None,
    }
    text = format_synthesis_message(row)
    assert "❓" in text
    assert "-1.50%" in text
    assert "Перевірити:" not in text


# --- Регресія 2026-10-02 (живий фідбек користувача): старий
# format_message() пакував УСІ рядки в ОДНЕ повідомлення -- разом із
# довшими summary/confirmation_factors (новий SYSTEM_PROMPT
# synthesize.py) кілька рядків одразу реально перевищували ліміт
# Telegram 4096 символів (HTTP 400 "message is too long", живо
# підтверджено). format_synthesis_message() тепер рахує ОДИН рядок --
# цей тест ловить регресію назад до пакетного формату.


def test_format_synthesis_message_single_row_stays_well_under_telegram_limit():
    row = {
        "asset_id": "xauusd",
        "cluster_count": 21,
        "net_lean": 7,
        "price_pct_change": Decimal("-3.43"),
        "price_start_date": date(2026, 9, 26),
        "price_end_date": date(2026, 10, 2),
        "direction": "down",
        "confidence": 0.65,
        # Реалістична довжина живого summary (2-3 речення) -- див.
        # docs/decisions.md 2026-10-02.
        "summary": "Ціна золота впала на 3.43% за період з 26 вересня по 2 жовтня 2026 року "
                   "(з 4286.21 до 4139.29). Серед 21 незалежної новини переважають позитивні "
                   "для золота сигнали, але фактичний рух ціни був низхідним. Конкретні причини "
                   "падіння: тиск високих дохідностей казначейських облігацій США та обережніший "
                   "прогноз Bank of America. Гіпотеза: рух виглядає як корекція після досягнення "
                   "історичного максимуму.",
        "confirmation_factors": "Якщо падіння продовжиться без нових ведмежих новин, або ціна "
                                 "відновиться попри позитивний фон -- це підтвердить гіпотезу корекції.",
    }
    text = format_synthesis_message(row)
    assert len(text) < 4096
