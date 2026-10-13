"""
Спільний інтерфейс для адаптерів новин — паралельний до common/adapter.py,
НЕ той самий: NormalizedRecord.value — Decimal (числовий показник),
новина текстова, туди не лягає. Той самий принцип розділення
fetch()/normalize() і той самий сенс observed_at/fetched_at (тут —
published_at/fetched_at), докладніше docs/decisions.md, 2026-09-25
"news/ — обсяг, межа шарів і схема БД".

Адаптер тут теж НІЧОГО не інтерпретує (rule 1: data-ingestion лише
збирає й нормалізує) — "stream" визначає тільки МЕХАНІЧНЕ звуження
джерела (які query-параметри використати), не оцінку важливості.
Чи новина релевантна — вирішує analysis/news/ (DeepSeek), не сюди.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

# Три потоки новин (одна логіка збору, різні query-параметри per
# stream) — див. docs/decisions.md, 2026-09-25.
STREAMS = frozenset({"watchlist", "general", "geopolitical"})


@dataclass
class NewsRecord:
    source: str
    external_id: str       # дедуп-ключ джерела (URL/guid)
    stream: str
    title: str
    url: str
    published_at: datetime
    fetched_at: datetime
    raw_payload: Optional[dict] = None


class BaseNewsAdapter(ABC):
    """Мінімальний контракт адаптера новин.

    fetch()     — тільки мережевий виклик, повертає сиру відповідь як є.
    normalize() — тільки перетворення сирої відповіді в NewsRecord[],
                  без побічних ефектів (без мережі, без БД) — щоб можна
                  було тестувати на фейкових прикладах відповіді.
    """

    source: str

    @abstractmethod
    def fetch(self, **kwargs) -> Any:
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw_response: Any) -> list[NewsRecord]:
        raise NotImplementedError

    def collect(self, **kwargs) -> list[NewsRecord]:
        """Наскрізний виклик: fetch → normalize."""
        return self.normalize(self.fetch(**kwargs))
