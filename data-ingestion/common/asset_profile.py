"""
Профіль активу списку обраних — ФАКТИ про актив, які зберігаються в
його рядку `watchlist_assets` (db/schema.sql, 2026-10-22): клас, група
товару, назва компанії, терміни пошуку, ланцюжок джерел ціни.

Чисті функції без мережі й БД: на вхід — те, що вже сказало джерело
(тип інструмента з Twelve Data `symbol_search`, назва з SEC), на вихід
— значення колонок. Мережу робить викликач (`/watchlist_add`,
`backfill_watchlist_profile.py`).

**Чому тут, а не в аналізі.** Таблиця тримає, ЩО це за актив (акція,
валютна пара, дорогоцінний метал). Що з цього ВИПЛИВАЄ (на який
макрореліз актив реагує) — вирішує аналіз за `config.ASSET_IMPACT_PROFILES`
(правило 1). Тому тут немає жодної категорії впливу.

**Терміни пошуку — ПРЕФІЛЬТР.** Хибний збіг дешевий (DeepSeek
відкине статтю), пропуск дорогий (новин про актив не буде зовсім).
Тому для акції беремо і назву компанії, і тикер.
"""

import re
from typing import Optional

ASSET_CLASSES = ("stock", "crypto", "fx", "commodity", "index")
COMMODITY_GROUPS = ("metal", "energy", "agri", "other")

# Twelve Data `instrument_type` (живо 2026-10-22: SKYD/AAPL "Common
# Stock", GLD "ETF", EUR/USD і GBP/USD "Physical Currency", BTC/USD
# "Digital Currency", XAU/USD "Precious Metal", WTI/USD "Energy
# Resource"). Порівняння в нижньому регістрі.
_STOCK_TYPES = frozenset({
    "common stock", "preferred stock", "etf", "depositary receipt",
    "american depositary receipt", "reit", "unit", "limited partnership",
    "closed-end fund", "trust",
})
_COMMODITY_GROUP_BY_TYPE = {
    "precious metal": "metal",
    "energy resource": "energy",
    "agricultural product": "agri",
    "agriculture": "agri",
    "industrial metal": "other",
    "base metal": "other",
    "livestock": "other",
}


def classify_instrument(instrument_type: Optional[str], exchange: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """(asset_class, commodity_group) за типом інструмента з Twelve Data.

    Невідомий тип — `(None, None)`, а не здогад: актив без класу видно
    в `/watchlist_list`, а хибний клас (як "товар" для акції SKYD)
    тихо псує все, що з нього випливає."""
    kind = (instrument_type or "").strip().lower()
    venue = (exchange or "").strip().upper()

    if kind == "digital currency":
        return "crypto", None
    if kind == "physical currency" or venue == "PHYSICAL CURRENCY":
        return "fx", None
    if kind in _COMMODITY_GROUP_BY_TYPE:
        return "commodity", _COMMODITY_GROUP_BY_TYPE[kind]
    if venue == "COMMODITY":
        return "commodity", "other"
    if kind == "index":
        return "index", None
    if kind in _STOCK_TYPES or kind.endswith("stock"):
        return "stock", None
    return None, None


# Хвости офіційних назв, що не несуть змісту для пошуку: "Skydance
# Corporation Class B Common Stock" → "Skydance".
_NAME_NOISE = re.compile(
    r"\b(class [a-z]|common stock|ordinary shares|depositary shares|shares|"
    r"incorporated|inc|corporation|corp|company|co|plc|ltd|limited|"
    r"holdings?|group|n\.?v|s\.?a|ag|se|spot|futures|us dollar)\b\.?",
    re.IGNORECASE,
)


def clean_name(name: Optional[str]) -> Optional[str]:
    """Назва без юридичних хвостів і без "/ US Dollar" у товарів.
    None, якщо після чистки нічого не лишилось."""
    if not name:
        return None
    head = name.split(" / ")[0]
    cleaned = _NAME_NOISE.sub(" ", head)
    cleaned = re.sub(r"[,()]", " ", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" .-&")
    return cleaned or None


def _word(text: str) -> str:
    return rf"\b{re.escape(text.lower())}\b"


def _pair_forms(ticker: str) -> list[str]:
    """'GBP/USD' → ['gbp/usd', '\\bgbpusd\\b']."""
    lowered = ticker.lower()
    forms = [re.escape(lowered)]
    if "/" in lowered:
        forms.append(_word(lowered.replace("/", "")))
    return forms


def crypto_base(ticker: Optional[str], asset_id: str) -> str:
    """'BNBUSDT' / 'BTC/USD' / None → 'BNB' / 'BTC' / ASSET_ID."""
    base = (ticker or asset_id).upper().split("/")[0]
    for quote in ("USDT", "USDC", "USD"):
        if base.endswith(quote) and len(base) > len(quote):
            return base[: -len(quote)]
    return base


def news_patterns(asset_class: Optional[str], asset_id: str, ticker: Optional[str],
                  name: Optional[str]) -> list[str]:
    """Регулярки для пошуку активу в заголовках RSS (нижній регістр)."""
    patterns: list[str] = []
    clean = clean_name(name)

    if asset_class == "fx" and ticker:
        patterns.extend(_pair_forms(ticker))
    elif asset_class == "crypto":
        patterns.append(_word(crypto_base(ticker, asset_id)))
        if clean and clean.upper() != crypto_base(ticker, asset_id):
            patterns.append(_word(clean))
    else:
        if clean:
            patterns.append(_word(clean))
        # Тикер — лише від 3 літер: дволітерні ("GE", "BX") збігаються з
        # прийменниками й абревіатурами майже в кожному заголовку.
        symbol = (ticker or "").split("/")[0]
        if asset_class in ("stock", "index") and len(symbol) >= 3:
            patterns.append(_word(symbol))
        if "/" in (ticker or ""):
            patterns.extend(_pair_forms(ticker))

    if not patterns:
        patterns.append(_word(asset_id))
    return list(dict.fromkeys(patterns))


def ask_patterns(asset_class: Optional[str], asset_id: str, ticker: Optional[str],
                 name: Optional[str]) -> list[str]:
    """Регулярки для питань користувача: ті самі, що для новин, плюс
    сам asset_id ("що по skyd?"). Українських синонімів для нового
    активу вивести нізвідки — їх, якщо треба, дописують у рядок."""
    patterns = news_patterns(asset_class, asset_id, ticker, name)
    patterns.append(_word(asset_id))
    return list(dict.fromkeys(patterns))


def yahoo_symbol(asset_class: Optional[str], ticker: Optional[str], asset_id: str) -> Optional[str]:
    """Символ Yahoo для ДРУГОГО джерела ціни (звірка + резерв). None —
    коли формат символу не виводиться з тикера (товари, індекси:
    "XPT/USD" у Yahoo — "PL=F", і жодне правило цього не дасть)."""
    if asset_class == "stock" and ticker:
        return ticker.upper().replace(".", "-").replace("/", "-")
    if asset_class == "fx" and ticker and "/" in ticker:
        base, quote = ticker.upper().split("/", 1)
        return f"{base}{quote}=X"
    if asset_class == "crypto":
        return f"{crypto_base(ticker, asset_id)}-USD"
    return None


def chain_entry(source: str, metric_id: str, ticker: Optional[str]) -> dict:
    return {"source": source, "metric_id": metric_id, "ticker": ticker}
