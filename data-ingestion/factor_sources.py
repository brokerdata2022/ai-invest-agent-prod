"""
Реєстр "яким адаптером збирати кожне джерело факторного ряду"
(`config.FACTOR_SERIES`, розділ 27).

Конфіг каже ЩО (ряд, основне джерело, пари для звірки, пороги), цей
модуль — ЯК (клас адаптера). Розділено свідомо: `config.py` не має
імпортувати адаптери, а збір (`run_collect_factors.py`) і звірка
(`monitoring/crosscheck_factors.py`) мають брати перелік джерел з
одного місця, а не кожен зі своєї копії.
"""

import config
from common.adapter import BaseAdapter
from commodities.eia_adapter import EiaAdapter
from commodities.ice_coffee_adapter import IceCoffeeAdapter
from commodities.ishares_iau_adapter import IsharesIauAdapter
from commodities.spdr_gld_adapter import SpdrGldAdapter
from commodities.tradingeconomics_adapter import (
    INDICATOR_METRICS as TE_INDICATOR_METRICS,
    PERIOD_METRICS as TE_PERIOD_METRICS,
    TradingEconomicsAdapter,
    TradingEconomicsIndicatorAdapter,
    TradingEconomicsPeriodAdapter,
)
from commodities.usda_psd_adapter import UsdaPsdAdapter
from crypto.beacon_staking_adapter import BeaconStakingAdapter
from crypto.blockchain_info_adapter import BlockchainInfoAdapter
from crypto.coinmetrics_adapter import CoinMetricsAdapter
from crypto.defillama_adapter import DefiLlamaAdapter
from crypto.etherscan_adapter import EtherscanAdapter
from crypto.farside_adapter import FarsideAdapter
from crypto.solana_rpc_adapter import ENDPOINTS as SOLANA_ENDPOINTS, SolanaRpcAdapter
from macro.mof_japan_adapter import MofJapanAdapter
from quotes.yahoo_chart_adapter import YahooChartAdapter
from weather.met_norway_adapter import MetNorwayAdapter
from weather.open_meteo_adapter import OpenMeteoAdapter

ADAPTER_BY_SOURCE: dict[str, type[BaseAdapter]] = {
    "eia": EiaAdapter,
    "spdr_gld": SpdrGldAdapter,
    "yahoo_chart": YahooChartAdapter,
    "coinmetrics": CoinMetricsAdapter,
    "blockchain_info": BlockchainInfoAdapter,
    "etherscan": EtherscanAdapter,
    "farside": FarsideAdapter,
    "defillama": DefiLlamaAdapter,
    "open_meteo": OpenMeteoAdapter,
    "met_norway": MetNorwayAdapter,
    "ice": IceCoffeeAdapter,
    "usda_psd": UsdaPsdAdapter,
    "ishares_iau": IsharesIauAdapter,
    "mof_japan": MofJapanAdapter,
    "beacon_lighthouse": BeaconStakingAdapter,
}


def build_adapter(source: str, metric_id: str) -> BaseAdapter:
    # TradingEconomics має три формати сторінок: товар (ціна), тижнева
    # зміна запасів і "період ← попередній період" — різні класи на
    # одне джерело.
    if source == "tradingeconomics":
        if metric_id in TE_INDICATOR_METRICS:
            return TradingEconomicsIndicatorAdapter(metric_id)
        if metric_id in TE_PERIOD_METRICS:
            return TradingEconomicsPeriodAdapter(metric_id)
        return TradingEconomicsAdapter(metric_id)
    # Два RPC-вузли Solana — один клас, але РІЗНІ джерела (для звірки).
    if source in SOLANA_ENDPOINTS:
        return SolanaRpcAdapter(metric_id, source=source)
    if source not in ADAPTER_BY_SOURCE:
        raise ValueError(f"Немає адаптера для джерела {source!r} (factor_sources.ADAPTER_BY_SOURCE)")
    return ADAPTER_BY_SOURCE[source](metric_id)


def secondary_pairs(metric_id: str) -> list[dict]:
    """Пари для звірки ряду з уже підставленими дефолтами."""
    spec = config.FACTOR_SERIES[metric_id]
    return [
        {
            "source": item["source"],
            "metric_id": item.get("metric_id", metric_id),
            "compare": item.get("compare", "level"),
        }
        for item in spec["secondary"]
    ]


def collection_plan(only_metric: str | None = None) -> list[tuple[str, str]]:
    """Усі (джерело, metric_id), які треба зібрати: основне джерело
    кожного ряду + усі джерела звірки. Без звірочних рядів звірці не
    було б із чим порівнювати."""
    plan: list[tuple[str, str]] = []
    for metric_id, spec in config.FACTOR_SERIES.items():
        if only_metric and metric_id != only_metric:
            continue
        plan.append((spec["primary"], metric_id))
        for pair in secondary_pairs(metric_id):
            plan.append((pair["source"], pair["metric_id"]))
    return plan
