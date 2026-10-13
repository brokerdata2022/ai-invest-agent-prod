"""
Тести парсерів офіційних календарів — на фрагментах РЕАЛЬНОЇ розмітки,
знятої живими запитами 2026-10-13, без мережі в самих тестах.

Усі чотири джерела — HTML-скрейпінг офіційних сторінок, не API, тож
головне, що тут стережеться: (а) правильний вибір ДНЯ РІШЕННЯ (день 2
засідання, не день 1), (б) правильний рік там, де сторінка містить
кілька років, (в) голосна помилка замість тихого порожнього списку.
"""

from datetime import date

import pytest

from official_calendars import (
    OFFICIAL_DATE_SOURCES,
    fetch_official_dates,
    _month,
    _text,
)


def test_text_flattens_html_and_entities():
    assert _text("<dd>Euro&nbsp;area  <b>HICP</b>\n flash</dd>") == "Euro area HICP flash"


@pytest.mark.parametrize("token, expected", [
    ("Jan.", 1), ("Feb", 2), ("Mar.", 3), ("Apr.", 4), ("May", 5),
    ("June", 6), ("July", 7), ("Aug.", 8), ("Sept.", 9), ("Sep", 9),
    ("Oct.", 10), ("Nov.", 11), ("Dec.", 12),
])
def test_month_parses_every_boj_and_fed_spelling(token, expected):
    """BOJ пише "Sept."/"June"/"July" повністю, Fed — "September".
    Обидва варіанти мусять читатись, інакше частина засідань тихо
    зникає з календаря."""
    assert _month(token) == expected


def test_month_rejects_garbage():
    assert _month("Remarks") is None
    assert _month("") is None


def test_unemployment_has_no_official_source_by_design():
    """`eurozone_unemployment_rate` — дані Eurostat, не ECB; у
    статистичному календарі ECB їх немає (перевірено живим пошуком).
    Повертати None тут — ЧЕСНЕ обмеження: показник лишається на
    ForexFactory, і це задокументовано, а не замовчано."""
    assert fetch_official_dates("eurozone_unemployment_rate") is None


def test_unknown_metric_returns_none_not_error():
    assert fetch_official_dates("definitely_not_a_metric") is None


def test_every_source_covers_a_metric_that_had_no_date_before():
    """Сенс усього модуля: `eurozone_deposit_rate`, `japan_cpi`,
    `japan_policy_rate` і `fed_funds_rate` за весь час роботи агента
    НЕ отримали дати жодного разу (ForexFactory покриває лише
    поточний тиждень). Якщо хтось прибере джерело з мапи — цей тест
    впаде."""
    assert {"fed_funds_rate", "eurozone_deposit_rate",
            "japan_cpi", "japan_policy_rate"} <= set(OFFICIAL_DATE_SOURCES)


# ---- розбір РЕАЛЬНИХ фрагментів розмітки ----

def test_boj_takes_day_two_and_year_from_caption(monkeypatch):
    """Два живі баги в одному тесті.

    (1) День РІШЕННЯ — другий: `Oct. 29 (Thurs.), 30 (Fri.)` → 30.
    (2) Рік — із `<caption>`, не вгаданий. Перша версія брала
    "поточний рік, інакше наступний", і рядки 2027-го отримували
    2026-й: у списку з'являлись і 30 жовтня (правильно), і 29
    (день 2 ЖОВТНЕВОГО 2027 засідання)."""
    import official_calendars as oc

    html = """
    <h2>2026</h2>
    <table><caption>Table : 2026</caption>
      <tr><td>Oct. 29 (Thurs.), 30 (Fri.)</td><td>Oct. 30 (Fri.)</td></tr>
      <tr><td>Dec. 17 (Thurs.), 18 (Fri.)</td><td>-</td></tr>
    </table>
    <table><caption>Table : 2027</caption>
      <tr><td>Jan. 21 (Thurs.), 22 (Fri.)</td><td>Jan. 22 (Fri.)</td></tr>
    </table>
    """
    monkeypatch.setattr(oc, "_get", lambda url: html)
    result = oc.fetch_boj_policy_meetings(today=date(2026, 10, 13))

    assert result == [date(2026, 10, 30), date(2026, 12, 18), date(2027, 1, 22)]
    assert date(2026, 10, 29) not in result, "день 1 засідання не є днем рішення"


def test_jp_cpi_takes_national_column_not_tokyo(monkeypatch):
    """Живий баг: перша версія читала ПЛАСКИЙ текст рядка й брала
    другу знайдену пару "місяць число". Референсний місяць
    ("January, 2026") під патерн не підпадає — кома й 4-значний рік, —
    тож "друга пара" виявлялась датою ТОКІО, і в списку були і
    20 лютого (Японія, правильно), і 27 лютого (Токіо)."""
    import official_calendars as oc

    html = """
    <table>
      <tr><th>Survey month</th><th>Date of release</th><th>Survey month</th><th>Date of release</th></tr>
      <tr><td>December, 2025</td><td>January 23, 2026</td><td>January, 2026</td><td>January 30, 2026</td></tr>
      <tr><td>January, 2026</td><td>February 20</td><td>February</td><td>February 27</td></tr>
      <tr><td>February</td><td>March 24</td><td>March</td><td>March 31</td></tr>
    </table>
    """
    monkeypatch.setattr(oc, "_get", lambda url: html)
    result = oc.fetch_jp_cpi_releases(today=date(2026, 10, 13))

    assert result == [date(2026, 1, 23), date(2026, 2, 20), date(2026, 3, 24)]
    assert date(2026, 2, 27) not in result, "це дата Токіо, не національна"


def test_jp_cpi_year_rolls_over_when_month_decreases(monkeypatch):
    """Рік у рядках здебільшого відсутній — відновлюється
    хронологічно: місяць пішов назад (Dec → Jan) означає новий рік."""
    import official_calendars as oc

    html = """
    <table>
      <tr><td>November, 2026</td><td>December 18, 2026</td><td>x</td><td>y</td></tr>
      <tr><td>December</td><td>January 22</td><td>x</td><td>y</td></tr>
    </table>
    """
    monkeypatch.setattr(oc, "_get", lambda url: html)
    assert oc.fetch_jp_cpi_releases(today=date(2026, 12, 1)) == [
        date(2026, 12, 18), date(2027, 1, 22),
    ]


def test_ecb_council_skips_day_one_and_non_monetary(monkeypatch):
    """Ставку вирішують на 2-й день МОНЕТАРНОГО засідання.
    "non-monetary policy meeting" і "Day 1" мусять відкидатись."""
    import official_calendars as oc

    html = """
    <dl>
      <dt>28/10/2026</dt><dd>Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 1)</dd>
      <dt>29/10/2026</dt><dd>Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 2), followed by press conference</dd>
      <dt>25/11/2026</dt><dd>Governing Council of the ECB: non-monetary policy meeting (in Frankfurt)</dd>
      <dt>17/12/2026</dt><dd>Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 2), followed by press conference</dd>
    </dl>
    """
    monkeypatch.setattr(oc, "_get", lambda url: html)
    assert oc.fetch_ecb_policy_meetings() == [date(2026, 10, 29), date(2026, 12, 17)]


def test_ecb_hicp_prefers_flash_estimate(monkeypatch):
    """Ринок рухає FLASH (виходить ~1-го числа), а не повний реліз
    через два тижні — і саме flash прогнозує ForexFactory-консенсус,
    з яким ми потім порівнюємо факт."""
    import official_calendars as oc

    html = """
    <dl>
      <dt>02/10/2026 15:00</dt><dd>Euro area seasonally adjusted HICP flash estimate (Dataset: HICP)</dd>
      <dt>16/10/2026 12:00</dt><dd>Euro area seasonally adjusted HICP (Dataset: HICP)</dd>
      <dt>04/11/2026 15:00</dt><dd>Euro area seasonally adjusted HICP flash estimate (Dataset: HICP)</dd>
    </dl>
    """
    monkeypatch.setattr(oc, "_get", lambda url: html)
    result = oc.ecb_hicp_releases()

    assert result == [date(2026, 10, 2), date(2026, 11, 4)]
    assert date(2026, 10, 16) not in result, "повний реліз — уточнення, не подія"


def test_fomc_takes_second_day_and_handles_cross_month(monkeypatch):
    """`8-9*` → 9 (день рішення). `Apr/May` + `28-1` → 1 травня:
    міжмісячне засідання, день 2 належить ДРУГОМУ місяцю."""
    import official_calendars as oc

    html = """
    <h4><a id="1">2026 FOMC Meetings</a></h4>
    <div class="fomc-meeting__month">October</div><div class="fomc-meeting__date">27-28</div>
    <div class="fomc-meeting__month">December</div><div class="fomc-meeting__date">8-9*</div>
    <h4><a id="2">2027 FOMC Meetings</a></h4>
    <div class="fomc-meeting__month">Apr/May</div><div class="fomc-meeting__date">28-1</div>
    """
    monkeypatch.setattr(oc, "_get", lambda url: html)
    result = oc.fetch_fomc_meetings(today=date(2026, 10, 13))

    assert date(2026, 10, 28) in result
    assert date(2026, 12, 9) in result
    assert date(2027, 5, 1) in result, "міжмісячне засідання: день 2 у другому місяці"


@pytest.mark.parametrize("fetcher_name", [
    "fetch_ecb_statscal", "fetch_ecb_policy_meetings",
    "fetch_boj_policy_meetings", "fetch_jp_cpi_releases", "fetch_fomc_meetings",
])
def test_every_parser_raises_loudly_on_changed_structure(monkeypatch, fetcher_name):
    """Головне правило неофіційного джерела: тихий порожній список
    читається як "релізів немає" і ховає поломку на тижні — рівно та
    помилка, через яку мʼякий троттл GDELT місяць залишався
    непоміченим. Кожен парсер мусить ПАДАТИ."""
    import official_calendars as oc

    monkeypatch.setattr(oc, "_get", lambda url: "<html><body>redesigned</body></html>")
    with pytest.raises(ValueError):
        getattr(oc, fetcher_name)()
