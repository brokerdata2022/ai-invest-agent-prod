"""
Чек-лист чинників для БУДЬ-ЯКОГО активу списку обраних — включно з
доданими через Telegram (`/watchlist_add`).

## Навіщо (2026-10-20, рішення користувача)

"ШІ для кожного активу — не забувай, що список динамічний — готує свій
чек-лист". Доти чек-лист мали лише 11 активів із `config.ASSET_FACTORS`,
а новий актив аналізувався на універсальному наборі (ставка ФРС + 10Y
+ новини), тобто для BNB чи GBP/USD специфічних драйверів не було б
ніколи — без зміни коду.

## Як

1. **Каталог** — ряди, що РЕАЛЬНО є в БД і живі: макро з описом
   (`forecasting/llm_forecast.py:METRIC_DESCRIPTIONS`), факторні ряди
   (`config.FACTOR_SERIES`), ціни інших активів watchlist. Рівні
   індексів (CPI) — лише як `yoy:` (урок 2026-10-04).
2. **ШІ обирає** з каталогу 1-8 ключів і окремо називає драйвери, яких
   у каталозі немає (прогалини).
3. **Код перевіряє** кожен ключ проти каталогу; вигадані відкидаються
   й пишуться в `rejected` для аудиту ("LLM пропонує, код перевіряє" —
   той самий принцип, що гейт правдоподібності прогнозу).
4. Результат — у `asset_checklists`; складається НАНОВО лише тоді,
   коли змінився сам каталог (`catalog_fingerprint`): новий факторний
   ряд, новий актив у watchlist. Не щодня — і не платимо за той самий
   чек-лист щодня, і аналіз не "плаває" від прогону до прогону.

## Пріоритет

`config.ASSET_FACTORS` → чек-лист ШІ з БД → універсальний набір. Ручні
чек-листи лишаються першими: вони перевірені живими кейсами.
Універсальний — лише коли ШІ не зміг (збій LLM або жоден ключ не
пройшов перевірку), і LLM аналізу про це попереджається.

## Ціни інших активів — ключ `asset:<asset_id>`

А не `source:metric_id`: джерело ціни активу автоматично
перемикається (`common/watchlist_db.py:choose_freshest_source`), і
чек-лист із прибитим джерелом після перемикання читав би застиглий
ряд. `asset:` розвʼязується на ПОТОЧНЕ джерело під час збору чинників.
"""

import hashlib
import html
import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import config
from forecasting.llm_forecast import METRIC_DESCRIPTIONS
from llm_common import (
    LlmError,
    SynthesisResponseError,
    call_llm,
    log_llm_call,
    parse_json_object,
)

logger = logging.getLogger(__name__)

PURPOSE = "asset_checklist"

ORIGIN_CONFIG = "config"
ORIGIN_LLM = "llm"
ORIGIN_DEFAULT = "default"

# Додаються до кожного чек-листа завжди — ШІ їх не обирає. Для акції
# ще й блок компанії (`config.CHECKLIST_COMPANY_FACTORS`, 2026-10-22).
RESERVED_KEYS = ("price", "news", "synthesis") + tuple(
    key for key, _ in config.CHECKLIST_COMPANY_FACTORS
)


def is_stock(asset: dict) -> bool:
    return asset.get("asset_class") == "stock"

_MAX_LABEL_CHARS = 160
_MAX_GAP_CHARS = 160


class ChecklistError(ValueError):
    """ШІ не дав придатного чек-листа (замало валідних ключів)."""


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    label: str
    last_observed_at: date


@dataclass
class Checklist:
    asset_id: str
    specs: tuple[tuple[str, str], ...]
    gaps: tuple[str, ...]
    origin: str
    rejected: tuple[str, ...] = ()


# --- каталог ----------------------------------------------------------

def _candidate_pairs(asset: dict, watchlist: list[dict]) -> dict[tuple[str, str], tuple[str, str]]:
    """(source, metric_id) → (ключ чек-листа, підпис) для всіх рядів,
    які МОЖУТЬ бути в каталозі. Що з них реально живе — вирішує БД."""
    pairs: dict[tuple[str, str], tuple[str, str]] = {}

    for source in config.CHECKLIST_MACRO_SOURCES:
        for metric_id, description in METRIC_DESCRIPTIONS.items():
            plain = f"{source}:{metric_id}"
            if plain in config.FACTOR_YOY_METRICS:
                pairs[(source, metric_id)] = (f"yoy:{plain}", f"{description} → у чек-листі як РІЧНА зміна, %")
            else:
                pairs[(source, metric_id)] = (plain, description)

    for metric_id, spec in config.FACTOR_SERIES.items():
        pairs[(spec["primary"], metric_id)] = (f"{spec['primary']}:{metric_id}", spec["label"])

    for other in watchlist:
        if other["asset_id"] == asset["asset_id"]:
            continue
        pairs[(other["source"], other["metric_id"])] = (
            f"asset:{other['asset_id']}",
            f"Ціна активу зі списку обраних: {other.get('label') or other['asset_id']}",
        )
    return pairs


def build_catalog(conn, asset: dict, watchlist: list[dict], today: Optional[date] = None) -> list[CatalogEntry]:
    pairs = _candidate_pairs(asset, watchlist)
    since = (today or date.today()) - timedelta(days=config.CHECKLIST_CATALOG_MAX_AGE_DAYS)
    sources = sorted({s for s, _ in pairs})
    metrics = sorted({m for _, m in pairs})
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source, metric_id, MAX(observed_at)
            FROM raw_observations
            WHERE observed_at >= %s AND source = ANY(%s) AND metric_id = ANY(%s)
            GROUP BY source, metric_id
            """,
            (since, sources, metrics),
        )
        rows = cur.fetchall()

    entries: dict[str, CatalogEntry] = {}
    for source, metric_id, last in rows:
        if (source, metric_id) not in pairs:
            continue
        key, label = pairs[(source, metric_id)]
        entries[key] = CatalogEntry(key, label, last)
    return sorted(entries.values(), key=lambda e: e.key)


def catalog_fingerprint(catalog: list[CatalogEntry]) -> str:
    """Лише КЛЮЧІ, без дат: нова точка в ряді — не привід складати
    чек-лист наново, новий ряд чи новий актив — привід."""
    return hashlib.sha256("\n".join(sorted(e.key for e in catalog)).encode("utf-8")).hexdigest()


# --- промпт і розбір -------------------------------------------------

SYSTEM_PROMPT = (
    "Ти аналітик фінансових ринків. Тобі дають ОДИН актив зі списку "
    "обраних і КАТАЛОГ рядів даних, які реально є в нашій базі. Склади "
    "чек-лист чинників, які найсильніше впливають саме на цей актив — "
    "за ним потім робитиметься його щоденний фундаментальний аналіз."
    "\n\n"
    "Правила:\n"
    "1. Обирай ЛИШЕ ключі з каталогу, дослівно, разом із префіксом. "
    "Ключ, якого немає в каталозі, буде відкинуто.\n"
    f"2. Від 1 до {config.CHECKLIST_MAX_FACTORS} чинників, найважливіші "
    "першими. Кожен мусить мати зрозумілий механізм впливу на ЦЕЙ актив; "
    "не додавай чинник 'про всяк випадок'.\n"
    "3. Ціну самого активу, новинний фон і звʼязок ціни з новинами НЕ "
    "обирай — вони додаються завжди.\n"
    f"4. Окремо назви до {config.CHECKLIST_MAX_GAPS} КЛЮЧОВИХ драйверів "
    "цього активу, яких у каталозі НЕМАЄ (gaps), коротко українською. "
    "Це чесні прогалини: аналіз прямо скаже, що зроблений без них."
    "\n\n"
    "Відповідай ЛИШЕ JSON-обʼєктом: {\"asset_class\": рядок (напр. "
    "'криптовалюта', 'валютна пара', 'товар', 'акція', 'індекс'), "
    "\"factors\": [{\"key\": ключ із каталогу, \"label\": коротко "
    "українською — що це і як впливає на актив}], \"gaps\": [рядки]}."
)


def build_prompt(asset: dict, catalog: list[CatalogEntry]) -> str:
    lines = [
        f"Актив: {asset.get('label') or asset['asset_id']} ({asset['asset_id']})",
        f"Джерело ціни: {asset.get('source')}/{asset.get('metric_id')}",
        "",
        "КАТАЛОГ (ключ — що це — остання точка в нашій базі):",
    ]
    for entry in catalog:
        lines.append(f"- {entry.key} — {entry.label} — {entry.last_observed_at}")
    if is_stock(asset):
        lines.append("")
        lines.append(
            "Це АКЦІЯ: блок даних компанії (квартальна звітність SEC, тези "
            "з останнього 10-K/10-Q, корпоративні події 8-K, угоди інсайдерів "
            "Form 4, консенсус аналітиків і free float) додається до "
            "чек-листа завжди — не обирай його, обери макро- й галузеві "
            "чинники поверх нього."
        )
    lines.append("")
    lines.append("Склади чек-лист чинників для цього активу.")
    return "\n".join(lines)


def parse_response(raw_content: str, catalog_keys: set[str]) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """(обрані чинники, прогалини, відкинуті ключі). Кидає
    `ChecklistError`, якщо валідних чинників менше мінімуму."""
    data = parse_json_object(raw_content, error_class=ChecklistError)

    chosen: list[tuple[str, str]] = []
    rejected: list[str] = []
    seen: set[str] = set()
    for item in data.get("factors") or []:
        if isinstance(item, str):
            key, label = item.strip(), ""
        elif isinstance(item, dict):
            key, label = str(item.get("key") or "").strip(), str(item.get("label") or "").strip()
        else:
            continue
        if not key or key in RESERVED_KEYS or key in seen:
            continue
        if key not in catalog_keys:
            rejected.append(key)
            continue
        seen.add(key)
        chosen.append((key, label[:_MAX_LABEL_CHARS]))

    chosen = chosen[:config.CHECKLIST_MAX_FACTORS]
    if len(chosen) < config.CHECKLIST_MIN_FACTORS:
        raise ChecklistError(
            f"валідних чинників {len(chosen)} (мінімум {config.CHECKLIST_MIN_FACTORS}); "
            f"відкинуто як відсутні в каталозі: {rejected}"
        )

    gaps = [
        str(gap).strip()[:_MAX_GAP_CHARS]
        for gap in (data.get("gaps") or [])
        if isinstance(gap, str) and gap.strip()
    ][:config.CHECKLIST_MAX_GAPS]
    return chosen, gaps, rejected


def assemble_specs(asset: dict, chosen: list[tuple[str, str]], catalog_labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Ціна → обрані ШІ чинники → новини й синтез. Підпис ШІ порожній —
    береться опис із каталогу."""
    label = asset.get("label") or asset["asset_id"]
    middle = tuple((key, text or catalog_labels.get(key, key)) for key, text in chosen)
    company = config.CHECKLIST_COMPANY_FACTORS if is_stock(asset) else ()
    return (("price", f"Ціна: {label}"),) + company + middle + (
        ("news", "Новинний фон"),
        ("synthesis", "Звʼязок ціни з новинами"),
    )


# --- БД ---------------------------------------------------------------

def load_stored(conn, asset_id: str) -> Optional[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT factors, gaps, rejected, catalog_fingerprint, created_at
            FROM asset_checklists
            WHERE asset_id = %s
            """,
            (asset_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(("factors", "gaps", "rejected", "catalog_fingerprint", "created_at"), row))


def save(conn, asset_id: str, chosen: list[tuple[str, str]], gaps: list[str],
         rejected: list[str], fingerprint: str, llm_call_id: Optional[int]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO asset_checklists
                (asset_id, factors, gaps, rejected, catalog_fingerprint, llm_call_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (asset_id) DO UPDATE
               SET factors = EXCLUDED.factors,
                   gaps = EXCLUDED.gaps,
                   rejected = EXCLUDED.rejected,
                   catalog_fingerprint = EXCLUDED.catalog_fingerprint,
                   llm_call_id = EXCLUDED.llm_call_id,
                   created_at = now()
            """,
            (
                asset_id,
                json.dumps([list(pair) for pair in chosen], ensure_ascii=False),
                json.dumps(gaps, ensure_ascii=False),
                json.dumps(rejected, ensure_ascii=False),
                fingerprint,
                llm_call_id,
            ),
        )
    conn.commit()


# --- вирішення чек-листа ---------------------------------------------

def default_checklist(asset_id: str, stock: bool = False) -> Checklist:
    """Універсальний набір; для акції — з блоком компанії одразу після
    ціни (без нього аналіз акції на "ставці й новинах" — рівно те, що
    вийшло для SKYD 2026-10-22)."""
    specs = config.DEFAULT_ASSET_FACTORS
    if stock:
        specs = specs[:1] + config.CHECKLIST_COMPANY_FACTORS + specs[1:]
    return Checklist(asset_id, specs, (), ORIGIN_DEFAULT)


def _from_stored(asset: dict, stored: dict, catalog: list[CatalogEntry]) -> Checklist:
    labels = {e.key: e.label for e in catalog}
    chosen = [(key, label) for key, label in stored["factors"]]
    return Checklist(
        asset["asset_id"],
        assemble_specs(asset, chosen, labels),
        tuple(stored["gaps"] or ()),
        ORIGIN_LLM,
        tuple(stored.get("rejected") or ()),
    )


def generate(conn, asset: dict, catalog: list[CatalogEntry]) -> tuple[list[tuple[str, str]], list[str], list[str], int]:
    """Один виклик ШІ + обовʼязковий аудит-лог (правило 5) ДО розбору:
    збійну відповідь найцікавіше розбирати постфактум."""
    prompt = build_prompt(asset, catalog)
    llm = call_llm(prompt, SYSTEM_PROMPT, purpose=PURPOSE)
    llm_call_id = log_llm_call(conn, llm=llm, purpose=PURPOSE, prompt=prompt, source_ref=asset["asset_id"])
    chosen, gaps, rejected = parse_response(llm.text, {e.key for e in catalog})
    return chosen, gaps, rejected, llm_call_id


def resolve_checklist(conn, asset: dict, watchlist: list[dict],
                      allow_generate: bool = True, force: bool = False) -> Checklist:
    """Чек-лист для активу за пріоритетом: config → БД (якщо каталог
    не змінився) → згенерувати ШІ → застарілий з БД → універсальний.

    `allow_generate=False` — без виклику ШІ (сухий прогін аналізу):
    береться збережений, навіть якщо каталог відтоді змінився."""
    asset_id = asset["asset_id"]
    if asset_id in config.ASSET_FACTORS:
        return Checklist(
            asset_id, config.ASSET_FACTORS[asset_id],
            tuple(config.ASSET_FACTOR_GAPS.get(asset_id, ())), ORIGIN_CONFIG,
        )

    catalog = build_catalog(conn, asset, watchlist)
    fingerprint = catalog_fingerprint(catalog)
    stored = load_stored(conn, asset_id)

    if stored and not force and (stored["catalog_fingerprint"] == fingerprint or not allow_generate):
        return _from_stored(asset, stored, catalog)
    if not allow_generate:
        return default_checklist(asset_id, is_stock(asset))

    try:
        chosen, gaps, rejected, llm_call_id = generate(conn, asset, catalog)
    except (LlmError, SynthesisResponseError, ChecklistError) as error:
        conn.rollback()
        logger.error("%s: ШІ не склав чек-лист — %s", asset_id, error)
        if stored:
            logger.warning("%s: лишаю попередній чек-лист (каталог відтоді змінився)", asset_id)
            return _from_stored(asset, stored, catalog)
        return default_checklist(asset_id, is_stock(asset))

    if rejected:
        logger.warning("%s: відкинуто ключі поза каталогом: %s", asset_id, rejected)
    save(conn, asset_id, chosen, gaps, rejected, fingerprint, llm_call_id)
    logger.info(
        "%s: складено чек-лист — %d чинників, %d прогалин (каталог %d рядів)",
        asset_id, len(chosen), len(gaps), len(catalog),
    )
    labels = {e.key: e.label for e in catalog}
    return Checklist(asset_id, assemble_specs(asset, chosen, labels), tuple(gaps), ORIGIN_LLM, tuple(rejected))


def format_checklist(asset: dict, checklist: Checklist) -> str:
    """Повідомлення для Telegram — що агент тепер дивиться по активу.
    Підписи від ШІ екрануються: у них можуть бути '<' і '&'."""
    name = html.escape(asset.get("label") or asset["asset_id"])
    origin_text = {
        ORIGIN_CONFIG: "ручний чек-лист із конфіга",
        ORIGIN_LLM: "склав ШІ з даних, що є в базі; кожен ключ перевірено кодом",
        ORIGIN_DEFAULT: "⚠️ ШІ не зміг скласти — тимчасово універсальний набір",
    }[checklist.origin]
    lines = [f"🧭 <b>Чек-лист чинників: {name}</b>", f"<i>{origin_text}</i>", ""]
    for key, label in checklist.specs:
        lines.append(f"• {html.escape(label)}")
    if checklist.gaps:
        lines.append("")
        lines.append("<b>Чого в даних немає</b> (аналіз скаже, що зроблений без цього):")
        lines.extend(f"• {html.escape(gap)}" for gap in checklist.gaps)
    return "\n".join(lines)
