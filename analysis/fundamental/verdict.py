"""
ФІНАЛЬНИЙ ВЕРДИКТ фундаменталу — чиста частина (промпт + розбір), без
SQL і мережі.

**Навіщо (2026-10-24, рішення користувача: "скоротити Anthropic,
збільшити DeepSeek без втрати якості основних аналізів").** До цього
кожен актив watchlist і кожна топ-акція йшли окремим викликом Sonnet 5
— ~14 викликів на день, більше половини всіх викликів Anthropic. Тепер
аналіз КОЖНОГО активу робить DeepSeek (вхід той самий, промпт той
самий — `fundamental_llm.py`), а Anthropic робить те, що окремі аналізи
зробити не можуть у принципі: дивиться на ВСІ разом. Які висновки
головні сьогодні, де активи чи чинники суперечать один одному (золото
"вгору" при дорожчанні долара), на що зважати.

Вхід — стислі висновки (напрям, упевненість, summary, аргументи за і
проти), не сирі чинники: інакше цей виклик знову коштував би стільки ж,
скільки 14 окремих.
"""

from dataclasses import dataclass, field

from llm_common import SynthesisResponseError, parse_json_object

# Скільки пунктів максимум у кожному списку вердикту — одне
# повідомлення, і довгий список однаково ніхто не дочитає.
MAX_VERDICT_POINTS = 5

SYSTEM_PROMPT = (
    "Ти старший аналітик, що зводить денні фундаментальні висновки "
    "команди в один вердикт. Тобі дають ГОТОВІ висновки по кожному "
    "активу списку обраних (товари, валюти, крипта, акції): напрям, "
    "упевненість, короткий підсумок і аргументи за й проти. Біля "
    "частини стоїть позначка 'без змін з <дата>' — їхні входи відтоді "
    "не змінювались.\n\n"
    "Твоя задача — те, чого окремий аналіз не бачить: подивитись на ВСІ "
    "активи разом. Що сьогодні головне; де висновки СУПЕРЕЧАТЬ один "
    "одному або спільним чинникам (наприклад, золото 'вгору' при "
    "сильному долару, нафта 'вниз' при зростанні запасів, але 'вгору' "
    "за видобутком); які активи тягне той самий чинник (ставки, долар, "
    "ризик-апетит); на що звернути увагу найближчими днями.\n\n"
    "Спирайся ЛИШЕ на надані висновки, не вигадуй нових даних. Висновок "
    "із низькою впевненістю не подавай як головний.\n\n"
    "НІКОЛИ не давай торгових рекомендацій ('купити'/'продати'/"
    "'входити в позицію') і не називай цільових цін. Відповідай ЛИШЕ "
    "JSON-обʼєктом з полями: summary (3-4 речення: головне по списку "
    "сьогодні), key_points (МАСИВ рядків — найважливіші висновки з "
    f"назвою активу, максимум {MAX_VERDICT_POINTS}), conflicts (МАСИВ "
    "рядків — суперечності між активами чи чинниками; порожній, якщо "
    f"їх немає; максимум {MAX_VERDICT_POINTS}), watch (МАСИВ рядків — на "
    f"що звернути увагу найближчими днями, максимум {MAX_VERDICT_POINTS})."
)


@dataclass
class VerdictResult:
    summary: str
    key_points: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    watch: list[str] = field(default_factory=list)


def _points(raw) -> list[str]:
    points = []
    for item in raw or []:
        text = item if isinstance(item, str) else (
            str(item.get("point") or item.get("text") or "") if isinstance(item, dict) else ""
        )
        if text.strip():
            points.append(text.strip())
    return points[:MAX_VERDICT_POINTS]


def parse_verdict_response(raw_content: str) -> VerdictResult:
    data = parse_json_object(raw_content, error_class=SynthesisResponseError)
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise SynthesisResponseError(f"У вердикті немає summary: {data!r}")
    return VerdictResult(
        summary=summary.strip(),
        key_points=_points(data.get("key_points")),
        conflicts=_points(data.get("conflicts")),
        watch=_points(data.get("watch")),
    )


DIRECTION_LABEL = {
    "up": "вгору",
    "down": "вниз",
    "neutral": "нейтрально",
    "unclear": "неоднозначно",
}


def build_verdict_prompt(analyses: list[dict], today) -> str:
    """`analyses` — останній висновок по кожному активу:
    {asset_id, company_name, asset_kind, direction, confidence, summary,
    strengths, risks, created_at}. Висновок не сьогоднішній = входи
    відтоді не змінились (гейт змін, `analysis/change_gate.py`)."""
    lines = [f"Дата: {today}", "", "ВИСНОВКИ ПО АКТИВАХ:"]
    for row in analyses:
        name = row.get("company_name") or row["asset_id"]
        kind = "акція" if row.get("asset_kind") == "stock" else "актив списку"
        created = row["created_at"].date() if hasattr(row["created_at"], "date") else row["created_at"]
        unchanged = f" [без змін з {created}]" if created != today else ""
        lines.append("")
        lines.append(
            f"## {name} ({row['asset_id']}, {kind}){unchanged} — "
            f"{DIRECTION_LABEL.get(row['direction'], row['direction'])}, "
            f"упевненість {float(row['confidence']):.2f}"
        )
        lines.append(f"Підсумок: {row['summary']}")
        for label, key in (("За", "strengths"), ("Проти", "risks")):
            items = row.get(key) or []
            if items:
                lines.append(f"{label}: " + "; ".join(items))
    lines.append("")
    lines.append(
        "Зведи ці висновки в один вердикт: головне, суперечності між "
        "активами, на що зважати."
    )
    return "\n".join(lines)
