"""
Адаптер RSS-фідів (центробанки й статслужби — first-party; широкі
редакційні та бізнес-стрічки — агрегатори) — надійніше за GDELT, але
вужче за обсягом: тільки те, що джерело саме публікує у стрічці, без
пошукового запиту по темі.

RSS 2.0 <item>: title/link/pubDate — обов'язкові (без них запис не
нормалізується). description — опційно, не всі фіди його дають
(перевірено живо 2026-09-26: Fed дає, ECB — ні) — якщо є, йде в
raw_payload для аудиту (rule 6), окремого поля під нього в NewsRecord
нема (той самий набір полів, що й у GDELT-адаптера).

На відміну від GdeltAdapter, source — параметр конструктора, не
фіксований клас-атрибут: кожен RSS-фід (fed_rss/ecb_rss/...) — окреме
джерело (news/rss_feeds.py), той самий підхід, що й один клас
адаптера на кілька series/тикерів в інших модулях data-ingestion/.
"""

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from xml.etree import ElementTree

import requests

from common.news_adapter import BaseNewsAdapter, NewsRecord, STREAMS

logger = logging.getLogger(__name__)


def parse_pub_date(raw: str) -> Optional[datetime]:
    """Дата публікації з `<pubDate>`. Повертає None, якщо формат
    нерозпізнаний.

    ДВА сімейства форматів, бо живі фіди не домовились (виявлено
    прогоном 2026-10-05, docs/decisions.md):
    - RFC 822 ("Mon, 05 Oct 2026 16:18:38 GMT") — RSS 2.0 за
      специфікацією, так віддають Fed/ECB/BOJ/BBC/Guardian/CNBC;
    - ISO 8601 ("2026-09-23T01:52:26Z", "2026-10-05 16:18:38") —
      формально не за специфікацією RSS, але так віддають Yahoo
      Finance та Investing.com.

    Чому це важливо: до фіксу адаптер знав ЛИШЕ RFC 822, тож
    `yahoo_finance_rss` втрачав ВСІ 49 статей, а `investing_rss` — усі
    10, і в лозі це виглядало як "отримано 0 записів" при HTTP 200.
    Той самий клас тихого збою, що й мʼякий троттл GDELT того ж дня.

    Дата без часової зони трактується як UTC — не "локальна":
    локальна зона контейнера (UTC) і зона редакції не збігаються, і
    вгадувати її не можна, а зсув у кілька годин для вікна свіжості
    в 24г не критичний."""
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        pass

    # "Z" (Zulu) `datetime.fromisoformat` розуміє лише з Python 3.11+,
    # але контейнер на 3.12 — заміна на "+00:00" лишається для
    # сумісності й читабельності.
    candidate = raw.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


# Контактний User-Agent. Додано 2026-10-05 разом із бізнес-фідами
# потоку `general`: частина видань (Nasdaq, FT) віддає стрічку
# неохоче або інакше на дефолтний `python-requests/2.x`, і живу
# перевірку фідів робили саме з явним UA — без нього перевірка була б
# нечесною щодо продакшену. Той самий урок, що
# `SEC_EDGAR_USER_AGENT` (без контактного UA EDGAR віддає 403).
# Без env-змінної свідомо: тут немає жодного секрету, а назва агента
# однакова для всіх фідів.
USER_AGENT = "ai-invest-agent/1.0 (+https://github.com/; RSS reader)"

# Таймаут запиту. 45с, не 30: живий прогін 2026-10-05 показав, що
# `nasdaq_markets_rss` інколи віддає стрічку довше 30с (той самий URL
# у ручній пробі відповів нормально) — а збій одного фіда й так не
# зриває збір решти (run_collect_rss.py), тож ширший таймаут тут
# дешевший за втрачений фід.
REQUEST_TIMEOUT_SECONDS = 45


class RssAdapter(BaseNewsAdapter):
    def __init__(
        self,
        source: str,
        stream: str,
        feed_url: str,
        session: Optional[requests.Session] = None,
    ):
        if stream not in STREAMS:
            raise ValueError(f"Невідомий stream: {stream!r}. Доступні: {sorted(STREAMS)}")
        if not feed_url:
            raise ValueError("feed_url не може бути порожнім")
        self.source = source
        self.stream = stream
        self.feed_url = feed_url
        self.session = session or requests.Session()

    def fetch(self) -> Any:
        response = self.session.get(
            self.feed_url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        # Сирі байти, НЕ .text: живий запит до Fed (2026-09-26) показав
        # HTTP-заголовок без charset при UTF-8 BOM у тілі — requests за
        # RFC-дефолтом вгадує ISO-8859-1 для .text, BOM перетворюється
        # на сміттєві символи перед "<?xml...", XML стає невалідним.
        # ElementTree сам коректно розпізнає кодування й BOM з байтів.
        return response.content

    def normalize(self, raw_response: Any) -> list[NewsRecord]:
        fetched_at = datetime.now(timezone.utc)
        records: list[NewsRecord] = []

        try:
            root = ElementTree.fromstring(raw_response)
        except ElementTree.ParseError:
            logger.warning("Не вдалось розпарсити RSS з %s (%s) — невалідний XML", self.feed_url, self.source)
            return []

        for item in root.iter("item"):
            title = _child_text(item, "title")
            link = _child_text(item, "link")
            pub_date_raw = _child_text(item, "pubDate")

            if not title or not link or not pub_date_raw:
                logger.warning(
                    "Пропущено item без title/link/pubDate (%s): title=%r link=%r pubDate=%r",
                    self.source, title, link, pub_date_raw,
                )
                continue

            published_at = parse_pub_date(pub_date_raw)
            if published_at is None:
                logger.warning("Не вдалось розпарсити pubDate %r для %s (%s)", pub_date_raw, link, self.source)
                continue
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=timezone.utc)

            records.append(
                NewsRecord(
                    source=self.source,
                    external_id=link,
                    stream=self.stream,
                    title=title,
                    url=link,
                    published_at=published_at,
                    fetched_at=fetched_at,
                    raw_payload={
                        "title": title,
                        "link": link,
                        "pubDate": pub_date_raw,
                        "description": _child_text(item, "description"),
                    },
                )
            )

        return records


def _child_text(item: ElementTree.Element, tag: str) -> Optional[str]:
    el = item.find(tag)
    if el is None or el.text is None:
        return None
    return el.text.strip()
