"""
Виступи членів центробанків із фіду подій → `policy_speeches`.

**Чому цього не було.** Постановка користувача 2026-10-11: "ставка
прогнозується після виходу нових макроекономічних даних + новини +
виступи банків та голів комісій". Три входи з п'яти вже були, а
виступи — ні, хоч вони й ПРИХОДИЛИ: `economic_calendar.py`
(ForexFactory-фід) віддає їх разом із релізами, із датою, часом і
impact. Живий замір 13 жовтня — **31 виступ зі 110 подій** фіду
("FOMC Member Waller Speaks", "ECB President Lagarde Speaks", "MPC
Member Breeden Speaks"). Вони просто нікуди не писались: цикл
`refresh_calendar` шукав у фіді лише збіги з НАШИМИ 15 показниками, а
решту подій відкидав.

**Чому не `release_log`.** Виступ не є релізом ДАНИХ: у нього немає
значення, очікування й факту для порівняння, тобто весь контракт
`release_log` (pending→detected→processed, `expected_value`,
`compare_releases.py`) до нього не застосовний. Спільне лише джерело
дати.
"""

import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Події фіду, що є ВИСТУПОМ. "Speaks" — основна форма ForexFactory;
# "Testifies"/"Testimony" — свідчення в парламенті (той самий сорт
# події: публічна позиція посадовця щодо політики).
_SPEECH_PATTERN = re.compile(r"\b(speaks|speech|testifies|testimony)\b", re.I)

# Хто саме говорить → інституція. Визначає, до ЯКОЇ ставки виступ
# стосується: синтез ставки ФРС не має спиратись на виступ члена
# Банку Англії, навіть якщо той звучав переконливо.
_INSTITUTION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bFOMC\b|\bFed\b|Federal Reserve", "fed"),
    (r"\bECB\b|Lagarde|Governing Council", "ecb"),
    (r"\bBOJ\b|Bank of Japan|Ueda", "boj"),
    (r"\bMPC\b|\bBOE\b|Bank of England", "boe"),
    (r"\bSNB\b", "snb"),
    (r"\bRBA\b", "rba"),
    (r"\bBOC\b|Bank of Canada", "boc"),
)

# Титул/роль перед прізвищем — прибираємо, щоб лишилось ім'я.
_TITLE_NOISE = re.compile(
    r"\b(FOMC|MPC|ECB|BOJ|BOE|SNB|RBA|BOC|Fed|FED)\b|"
    r"\b(Member|President|Governor|Chair|Chairman|Vice|Deputy|Gov)\b\.?",
    re.I,
)


def is_speech(title: str) -> bool:
    return bool(_SPEECH_PATTERN.search(title or ""))


def detect_institution(title: str) -> Optional[str]:
    for pattern, institution in _INSTITUTION_PATTERNS:
        if re.search(pattern, title or ""):
            return institution
    return None


def extract_speaker(title: str) -> Optional[str]:
    """Ім'я доповідача з назви події.

    "FOMC Member Waller Speaks" → "Waller"; "ECB President Lagarde
    Speaks" → "Lagarde". Прибираємо дієслово й титули, лишається
    прізвище. None, якщо після чистки нічого змістового не лишилось —
    це нормально ("ECB Press Conference" не має доповідача)."""
    without_verb = _SPEECH_PATTERN.sub(" ", title or "")
    cleaned = _TITLE_NOISE.sub(" ", without_verb)
    # Лишаємо слова з великої літери — імена; службові слова вже зняті.
    words = [w for w in re.split(r"[\s,]+", cleaned) if w and w[0].isupper()]
    return " ".join(words) or None


def save_speeches(conn, calendar_events: list[dict]) -> int:
    """Виступи з фіду → `policy_speeches`. Повертає скільки НОВИХ
    записів додано.

    `ON CONFLICT DO NOTHING` за (title, scheduled_at): фід віддає ту
    саму подію на кожному прогоні, доки вона в межах тижневого вікна,
    і дублікати тут не потрібні."""
    rows = []
    for event in calendar_events:
        title = str(event.get("title") or "").strip()
        if not title or not is_speech(title):
            continue
        raw_date = event.get("date")
        if not raw_date:
            continue
        try:
            scheduled_at = datetime.fromisoformat(str(raw_date))
        except ValueError:
            logger.warning("Виступ %r: не розібрано дату %r — пропущено", title, raw_date)
            continue
        rows.append((
            "economic_calendar", title, extract_speaker(title),
            detect_institution(title), scheduled_at,
            (event.get("impact") or "").lower() or None,
        ))

    if not rows:
        logger.info("У фіді подій немає виступів")
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO policy_speeches
                (source, title, speaker, institution, scheduled_at, impact_level)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (title, scheduled_at) DO NOTHING
            """,
            rows,
        )
        inserted = cur.rowcount
    conn.commit()
    logger.info(
        "Виступи: %d у фіді, %d нових записано (решта вже були)",
        len(rows), max(inserted, 0),
    )
    return max(inserted, 0)


def fetch_recent_speeches(conn, institution: str, days_back: int, days_ahead: int) -> list[dict]:
    """Виступи цієї інституції у вікні [сьогодні-days_back,
    сьогодні+days_ahead] — вхід для синтезу ставки.

    Вікно двостороннє: минулі виступи кажуть, що посадовці ВЖЕ
    сказали (це і формує очікування ринку), майбутні — коли чекати
    нової інформації до засідання."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT title, speaker, scheduled_at, impact_level
            FROM policy_speeches
            WHERE institution = %s
              AND scheduled_at >= now() - make_interval(days => %s)
              AND scheduled_at <= now() + make_interval(days => %s)
            ORDER BY scheduled_at
            """,
            (institution, days_back, days_ahead),
        )
        columns = [c[0] for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
