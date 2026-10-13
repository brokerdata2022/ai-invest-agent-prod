"""
Спільний інтерфейс для всіх адаптерів джерел даних.

Кожен адаптер (FRED, SEC EDGAR, yfinance, CoinGecko, ...) реалізує
BaseAdapter і повертає список NormalizedRecord — незалежно від того,
яка структура відповіді в оригінального API.

Важливо (див. docs/decisions.md, "revision обчислюється шаром
збереження"): адаптер НЕ визначає revision сам — він не звертається
до БД і не знає історії. Поле revision тут завжди None; фактичний
номер ревізії проставляє common/db.py під час запису, порівнюючи
з уже збереженим значенням.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional


@dataclass
class NormalizedRecord:
    source: str
    metric_id: str
    value: Decimal
    observed_at: date
    fetched_at: datetime
    revision: Optional[int] = None
    raw_payload: Optional[dict] = None


class BaseAdapter(ABC):
    """Мінімальний контракт адаптера джерела даних.

    fetch()     — тільки мережевий виклик, повертає сиру відповідь як є.
    normalize() — тільки перетворення сирої відповіді в NormalizedRecord[],
                  без побічних ефектів (без мережі, без БД) — щоб можна
                  було тестувати на фейкових прикладах відповіді.
    """

    source: str

    @abstractmethod
    def fetch(self, **kwargs) -> Any:
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw_response: Any) -> list[NormalizedRecord]:
        raise NotImplementedError

    def collect(self, **kwargs) -> list[NormalizedRecord]:
        """Наскрізний виклик: fetch → normalize. Це те, що викликають
        зовнішні скрипти (run_collect.py); fetch/normalize окремо
        викликаються тільки в тестах."""
        return self.normalize(self.fetch(**kwargs))
