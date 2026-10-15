"""
Про ЩО питання — чиста функція (rule 1, CLAUDE.md): SQL — `_db.py`,
проводка — `answer_question.py`.

**Чому не LLM.** Визначити, що «чому золото впало» стосується
`xauusd`, можна словником — а виклик моделі для цього коштував би
стільки само, скільки сама відповідь, і додав би ще одну точку відмови
(analysis/CLAUDE.md: не викликати LLM для того, що рахується кодом).
LLM лишається там, де справді потрібен — зводить знайдені дані в
речення.

**Два види згадок.** Watchlist-активи шукаються за синонімами
(`config.ASSISTANT_ASSET_TERMS`, українські й англійські), тикери
акцій — за формою слова (2-5 великих літер) із перевіркою проти того,
що ми реально ведемо. Друге важливо: інакше «ЧОМУ» або «ВВП» у питанні
стали б «тикерами» й потягнули б порожній контекст.
"""

import re
from typing import Optional

# Тикер: 2-5 великих латинських літер окремим словом. Довші й
# коротші послідовності — майже завжди не тикер (I, A, NASDAQ).
_TICKER = re.compile(r"\b[A-Z]{2,5}\b")

# Слова, що виглядають як тикер, але ним не є. Перелік короткий
# свідомо: головний фільтр — звірка з тим, що ми ведемо
# (`known_tickers`), а це лише дешеве відсікання найчастішого.
_NOT_TICKERS = frozenset({
    "USD", "EUR", "JPY", "UAH", "LLM", "CPI", "GDP", "PCE", "FOMC",
    "ECB", "BOJ", "FED", "ETF", "SQL", "API", "USA", "OK",
})


def _normalized(text: str) -> str:
    return text.lower().replace("’", "'")


def resolve_assets(question: str, asset_terms: dict, limit: int) -> list[str]:
    """asset_id watchlist-активів, згаданих у питанні.

    `asset_terms` — РЕГУЛЯРКИ зі стемами
    (`config.ASSISTANT_ASSET_TERMS`), не перелік словоформ: українська
    словозміна дає «нафта/нафти/нафтою/нафті», і перелік форм
    гарантовано щось пропустить — живо пропустив «єні» й «нафтою»
    (падіння тестів 2026-10-15).

    Порядок — за позицією першої згадки в тексті: якщо питання про
    «нафту й золото», нафта має бути першою і в контексті, бо
    обрізання контексту ріже з кінця."""
    haystack = _normalized(question)
    hits: list[tuple[int, str]] = []

    for asset_id, patterns in asset_terms.items():
        positions = [
            match.start()
            for match in (re.search(pattern, haystack) for pattern in patterns)
            if match is not None
        ]
        if positions:
            hits.append((min(positions), asset_id))

    hits.sort()
    return [asset_id for _pos, asset_id in hits[:limit]]


def resolve_tickers(question: str, known_tickers: set, limit: int) -> list[str]:
    """Тикери акцій із питання — лише ті, які ми справді ведемо.

    `known_tickers` приходить із БД (`_db.known_tickers()`), а не з
    конфіга: універсум змінюється зі складом S&P, і хардкод тут
    застарівав би тихо."""
    seen: list[str] = []
    for match in _TICKER.finditer(question):
        candidate = match.group(0)
        if candidate in _NOT_TICKERS or candidate not in known_tickers:
            continue
        if candidate not in seen:
            seen.append(candidate)
        if len(seen) >= limit:
            break
    return seen


def looks_like_question(text: str) -> Optional[str]:
    """Текст, який варто вважати питанням до агента, або None.

    Відсікається порожнє, самі команди (`/...` обробляє
    `telegram_commands.py` своєю гілкою) і дуже короткі репліки на
    кшталт «ок»/«дякую»: кожне питання — платний виклик LLM, і
    відповідати на «ок» означало б палити гроші на ввічливість.
    """
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned or cleaned.startswith("/"):
        return None
    if len(cleaned) < 8:
        return None
    return cleaned
