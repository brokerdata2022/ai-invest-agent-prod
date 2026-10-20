"""
Фундаментальний LLM-аналіз акції — ЧИСТА частина (промпт + розбір
відповіді), без SQL і без мережі (той самий стиль, що
`forecasting/llm_forecast.py` і `trading_list/scoring.py`).

## Чим це НЕ є

Не `news_analysis/synthesize.py` — там LLM зіставляє новинний фон із
рухом ціни (Цілі 1/2). Тут новин немає взагалі: вхід — квартальні
числа звітності з SEC EDGAR (виручка, прибуток, EPS, активи,
зобовʼязання, акції в обігу) плюс уже порахована оцінка зі скринінгу
(P/E, темпи зростання). Питання інше: що кажуть ЦИФРИ КОМПАНІЇ.

Не `screening/` — скринінг детермінований і відповідає "пройшов чи
ні" за порогами. Тут інтерпретація: чому зростання сповільнюється, чи
розмивається частка акціонера, чи борг під контролем.

## Чому це законний LLM-виклик

`analysis/CLAUDE.md` забороняє LLM там, де є формула. Тут формули
немає: темпи й P/E уже порахував звичайний код (`screening/`), а LLM
робить саме те, для чого призначений — зводить кілька різнорідних
сигналів у звʼязний висновок із сильними сторонами й ризиками.

## Формат виходу

Розширює стандартний контракт (direction/confidence/summary/reasoning)
двома полями: `strengths` і `risks`. Вони — суть фундаментального
висновку, і мають бути СПИСКАМИ, а не абзацем: інакше їх неможливо
показати окремо й неможливо порівняти між тикерами.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from llm_common import (
    DIRECTIONS,
    SYNTHESIS_FIELDS,
    SynthesisResponseError,
    parse_confidence,
    parse_json_object,
)

# Скільки пунктів максимум беремо зі strengths/risks. Обрізання тут, а
# не в reporting/: довгий список однаково не вміститься в повідомлення,
# а зберігати 20 пунктів "про запас" немає кому читати.
MAX_POINTS = 4

SYSTEM_PROMPT = (
    "Ти фундаментальний аналітик акцій. Тобі дають квартальну "
    "звітність ОДНІЄЇ компанії за кілька років (виручка, чистий "
    "прибуток, EPS, активи, зобовʼязання, кількість акцій в обігу) і "
    "вже пораховані показники оцінки (P/E, темпи зростання виручки й "
    "EPS рік-до-року)."
    "\n\n"
    "Твоя задача — сказати, що ці ЦИФРИ означають для компанії. "
    "Дивись саме на динаміку, а не на абсолютні рівні: чи зростання "
    "прискорюється або сповільнюється; чи прибуток росте разом із "
    "виручкою (чи маржа стискається); чи EPS зростає швидше за "
    "прибуток (викуп акцій) або повільніше (розмиття частки "
    "акціонера); як змінюється співвідношення зобовʼязань до активів; "
    "чи оцінка (P/E) виправдана темпами."
    "\n\n"
    "Будь конкретним і спирайся на НАДАНІ числа — називай періоди й "
    "величини. Якщо даних для якогось висновку немає, прямо скажи це, "
    "а не вигадуй: 'зобовʼязання за один період, динаміку оцінити "
    "неможливо' краще за припущення."
    "\n\n"
    "НІКОЛИ не давай торгових рекомендацій ('купити'/'продати'/"
    "'входити в позицію') і не називай цільових цін — тільки опис "
    "стану бізнесу. Відповідай ЛИШЕ JSON-обʼєктом з полями: direction "
    "(одне з: up, down, neutral, unclear — куди вказує ФУНДАМЕНТАЛЬНА "
    "картина бізнесу, не прогноз ціни), confidence (число від 0 до 1 — "
    "наскільки однозначна картина), summary (1-2 речення: головне про "
    "стан бізнесу), reasoning (коротке обґрунтування для аудиту), "
    "strengths (МАСИВ рядків — сильні сторони, по одному пункту, "
    f"максимум {MAX_POINTS}; порожній масив, якщо їх немає), risks "
    f"(МАСИВ рядків — ризики й слабкі місця, максимум {MAX_POINTS}; "
    "порожній масив, якщо їх немає)."
)


@dataclass
class FundamentalResult:
    direction: str
    confidence: float
    summary: str
    reasoning: str
    strengths: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


def _parse_points(raw, label: str) -> list[str]:
    """Масив рядків зі відповіді LLM. На відміну від основних полів,
    некоректний елемент НЕ валить увесь аналіз — пропускається з
    логом-подібним поводженням (той самий принцип, що
    expectations/synthesize.py:_parse_impacts): summary/direction
    лишаються корисними навіть без повного списку."""
    points: list[str] = []
    for item in raw or []:
        if isinstance(item, str) and item.strip():
            points.append(item.strip())
        elif isinstance(item, dict):
            # LLM іноді віддає [{"point": "..."}] замість ["..."] —
            # дешевше прийняти, ніж відкинути корисний вміст.
            text = str(item.get("point") or item.get("text") or "").strip()
            if text:
                points.append(text)
    return points[:MAX_POINTS]


def parse_fundamental_response(raw_content: str) -> FundamentalResult:
    data = parse_json_object(raw_content, error_class=SynthesisResponseError)

    missing = [f for f in SYNTHESIS_FIELDS if f not in data]
    if missing:
        raise SynthesisResponseError(f"У відповіді LLM бракує полів {missing}: {data!r}")

    direction = data["direction"]
    if direction not in DIRECTIONS:
        raise SynthesisResponseError(f"Неочікуване значення direction: {direction!r}")

    return FundamentalResult(
        direction=direction,
        confidence=parse_confidence(data["confidence"], error_class=SynthesisResponseError),
        summary=data["summary"],
        reasoning=data["reasoning"],
        strengths=_parse_points(data.get("strengths"), "strengths"),
        risks=_parse_points(data.get("risks"), "risks"),
    )


def _fmt(value) -> str:
    """Велике число читабельно: 12345678901 → 12.35 млрд. LLM краще
    тлумачить порядок, коли він названий словом, а не 11 цифрами."""
    try:
        number = Decimal(str(value))
    except Exception:
        return str(value)

    magnitude = abs(number)
    if magnitude >= Decimal("1e9"):
        return f"{number / Decimal('1e9'):.2f} млрд"
    if magnitude >= Decimal("1e6"):
        return f"{number / Decimal('1e6'):.2f} млн"
    if magnitude >= Decimal("1000"):
        return f"{number / Decimal('1000'):.2f} тис"
    return f"{number:.4g}"


def build_prompt(
    ticker: str,
    company_name: Optional[str],
    series: dict[str, list[dict]],
    valuation: Optional[dict] = None,
    filing_insight: Optional[dict] = None,
) -> str:
    """`series` — {людська назва показника: [{observed_at, value}, ...]}
    у ХРОНОЛОГІЧНОМУ порядку (найстаріше перше).
    `valuation` — уже порахований зріз зі `screening_results`
    (pe/revenue_growth/eps_growth); None, якщо тикера там немає.
    `filing_insight` — тези з ТЕКСТУ останнього 10-K/10-Q
    (`filing_insights`), додано 2026-10-13.

    **Навіщо `filing_insight`.** Живий вивід 12 жовтня показав межу
    цього аналізу: він переказував цифри ("виручка зросла з 3.69 до
    5.04 млрд") і для кожної з 5 акцій писав ті самі чотири ризики
    ("волатильний прибуток", "разові статті", "P/E закладає ріст"),
    бо з самих чисел причину вивести НЕМОЖЛИВО. Причина написана
    словами в MD&A, і тепер вона в промпті: на тому ж AAPL витяг дав
    "зростання iPhone Pro у всіх регіонах", "повернення сплачених
    тарифів після рішення Верховного суду", "посилення формулювання
    про дефіцит NAND/DRAM" — те, чого в числах немає взагалі."""
    header = f"Компанія: {company_name or ticker} ({ticker})"
    lines = [header, ""]

    if valuation:
        lines.append("Оцінка й темпи (пораховані нашим скринінгом):")
        if valuation.get("pe") is not None:
            lines.append(f"- P/E: {float(valuation['pe']):.2f}")
        if valuation.get("revenue_growth") is not None:
            lines.append(
                f"- Зростання виручки рік-до-року: {float(valuation['revenue_growth']) * 100:+.1f}%"
            )
        if valuation.get("eps_growth") is not None:
            lines.append(
                f"- Зростання EPS рік-до-року: {float(valuation['eps_growth']) * 100:+.1f}%"
            )
        lines.append("")

    lines.append("Квартальна звітність (від найстарішого до найновішого):")
    for label, points in series.items():
        if not points:
            continue
        rendered = ", ".join(
            f"{p['observed_at']}: {_fmt(p['value'])}" for p in points
        )
        lines.append(f"- {label} — {rendered}")

    if filing_insight:
        lines.append("")
        lines.append(
            f"Що САМА КОМПАНІЯ пише в останньому звіті "
            f"({filing_insight.get('form', '10-K/10-Q')}, "
            f"{filing_insight.get('filing_date', '')}):"
        )
        if filing_insight.get("summary"):
            lines.append(f"- Головне: {filing_insight['summary']}")
        for key, label in (
            ("drivers", "Чинники, які керівництво називає причинами"),
            ("risks", "Ризики, які компанія вважає суттєвими"),
            ("changes", "Що ЗМІНИЛОСЬ у формулюваннях відносно попереднього звіту"),
        ):
            items = filing_insight.get(key) or []
            if items:
                lines.append(f"- {label}:")
                lines.extend(f"  • {item}" for item in items)

    lines.append("")
    lines.append(
        "Проаналізуй динаміку цих показників і скажи, що вони означають "
        "для стану бізнесу."
    )
    if filing_insight:
        lines.append(
            "Спирайся на формулювання зі звіту там, де вони пояснюють "
            "цифри: саме вони відрізняють причину від збігу. Окремо "
            "зазнач, якщо цифри й слова компанії РОЗХОДЯТЬСЯ — це "
            "сильніший сигнал за будь-який з них окремо."
        )
    return "\n".join(lines)


# ============================================================
# РОЗГОРНУТИЙ АНАЛІЗ АКТИВА ЗА ЙОГО ЧИННИКАМИ
# ============================================================
# Рішення користувача 2026-10-04: "фундаментальний аналіз це має бути
# розгорнутий аналіз по активах із списку вибраних і кожний актив
# аналізується окремо згідно їхніх чинників".
#
# Відмінність від SYSTEM_PROMPT вище (акції): там вхід — звітність
# компанії, однакова за структурою для будь-якого тикера. Тут у
# КОЖНОГО активу СВІЙ набір чинників (золото — реальні ставки й долар,
# USD/JPY — різниця ставок ФРС/БОЯ, нафта — економічна активність), і
# промпт мусить просити зіставити саме їх між собою, а не "оцінити
# показники" абстрактно.

ASSET_SYSTEM_PROMPT = (
    "Ти фундаментальний аналітик фінансових ринків. Тобі дають ОДИН "
    "актив і перелік ЧИННИКІВ, що саме на нього впливають — з "
    "останніми значеннями кожного. Набір чинників різний для різних "
    "активів: для золота це реальні ставки й сила долара, для USD/JPY "
    "різниця ставок ФРС і Банку Японії, для нафти економічна "
    "активність."
    "\n\n"
    "Твоя задача — РОЗГОРНУТИЙ аналіз саме цього активу: пройди по "
    "КОЖНОМУ наданому чиннику, скажи, у який бік він тисне на актив "
    "ЗАРАЗ і чому, а потім зведи їх разом — які чинники сильніші, чи "
    "вони узгоджені, чи суперечать один одному. Спирайся на НАДАНІ "
    "числа: називай значення й напрямок зміни, а не загальні слова."
    "\n\n"
    "КРИТИЧНО про межі даних: частину чинників нам узагалі недоступно "
    "(вони названі окремо як 'чинники, яких ми не маємо'), а частина "
    "може бути без даних. НЕ вдавай, що врахував їх, і не вигадуй "
    "їхніх значень — прямо скажи, що висновок зроблений без них і чого "
    "саме бракує для повноти. Висновок із честно названою межею "
    "кращий за впевнений висновок на вигаданих даних."
    "\n\n"
    "Біля частини чинників у квадратних дужках стоїть ПОЗНАЧКА ДОВІРИ: "
    "'звірено' — значення підтвердило незалежне друге джерело; 'одне "
    "джерело' — звірити нема з чим, тож вага чинника нижча; 'ДЖЕРЕЛА "
    "РОЗІЙШЛИСЬ' — значення сумнівне; для прогнозів погоди це означає "
    "невизначеність прогнозу; 'ЗАСТАРІЛО' — нових даних давно немає. На "
    "сумнівних і застарілих чинниках НЕ будуй головний висновок і прямо "
    "згадай це в reasoning."
    "\n\n"
    "НІКОЛИ не давай торгових рекомендацій ('купити'/'продати'/"
    "'входити в позицію') і не називай цільових цін — тільки опис того, "
    "куди тиснуть чинники. Відповідай ЛИШЕ JSON-обʼєктом з полями: "
    "direction (одне з: up, down, neutral, unclear — куди вказує "
    "СУКУПНІСТЬ чинників), confidence (число від 0 до 1 — наскільки "
    "чинники узгоджені між собою), summary (2-3 речення: головне про "
    "актив і що зараз вирішальне), reasoning (розбір по чинниках: "
    "який у який бік тисне — саме це користувач читає, щоб зрозуміти "
    "висновок), strengths (МАСИВ рядків — чинники НА КОРИСТЬ "
    f"зростання, максимум {MAX_POINTS}), risks (МАСИВ рядків — чинники "
    f"ПРОТИ, максимум {MAX_POINTS}; сюди ж — важливі прогалини в даних)."
)


def build_asset_prompt(asset_factors) -> str:
    """Промпт для розгорнутого аналізу активу за його чинниками.
    `asset_factors` — `factors.AssetFactors`.

    Недоступні чинники перелічуються ЯВНО, а не замовчуються: інакше
    LLM не знає, чи чинник неважливий, чи просто не дістався — і
    схильний домислювати."""
    lines = [f"Актив: {asset_factors.label} ({asset_factors.asset_id})", ""]

    lines.append("ЧИННИКИ ВПЛИВУ (останні значення, від старішого до новішого):")
    for factor in asset_factors.available:
        if factor.points:
            rendered = ", ".join(
                f"{p['observed_at']}: {_fmt(p['value'])}" for p in factor.points
            )
            note = f" [{factor.note}]" if factor.note else ""
            lines.append(f"- {factor.label} — {rendered}{note}")
        else:
            lines.append(f"- {factor.label}:")
            for text in factor.texts:
                lines.append(f"    • {text}")

    unavailable = asset_factors.unavailable
    if unavailable:
        lines.append("")
        lines.append("ЧИННИКИ БЕЗ ДАНИХ (врахувати як обмеження висновку):")
        for factor in unavailable:
            lines.append(f"- {factor.label} — {factor.missing_reason}")

    if asset_factors.gaps:
        lines.append("")
        lines.append(
            "ЧИННИКИ, ЯКИХ МИ НЕ МАЄМО ВЗАГАЛІ (не збираються нашою "
            "системою — не вигадуй їхніх значень):"
        )
        for gap in asset_factors.gaps:
            lines.append(f"- {gap}")

    lines.append("")
    lines.append(
        "Зроби розгорнутий аналіз цього активу: пройди по кожному чиннику "
        "окремо, потім зведи їх у спільний висновок."
    )
    return "\n".join(lines)
