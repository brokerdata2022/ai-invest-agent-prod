"""
Гейт змін перед LLM-викликом (2026-10-24, рішення користувача:
"скоротити Anthropic без втрати якості").

**Навіщо.** Частина аналізів щодня отримувала ті самі входи: звітність
акції змінюється раз на квартал, запаси нафти раз на тиждень, а ставка
між засіданнями стоїть. Модель усе одно викликалась і писала той самий
висновок іншими словами — за гроші, і ще одним повідомленням у
Telegram.

**Як.** Входи виклику діляться на дві частини:
- `stable` — те, що має збігатися ДОСЛІВНО (дати й значення релізів,
  тексти новин, список виступів). Будь-яка зміна = нові дані.
- `anchors` — числа, що рухаються щодня (ціна, дохідність). Тут
  "змінилось" означає "зрушило далі за поріг відносно ТОГО значення,
  на якому робився минулий аналіз" — а не відносно вчорашнього, інакше
  повільний дрейф ніколи б не спрацював.

Висновок, зроблений на незмінних входах, перевикористовується; але не
довше за `config.CHANGE_GATE_MAX_AGE_DAYS` — тоді аналіз робиться
наново, навіть якщо входи стоять (щоб висновок не застарів тихо).

Відбиток запам'ятовується лише ПІСЛЯ успішного збереження висновку
(`remember`): збійний виклик не має виглядати як "вже зроблено".
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import config


@dataclass(frozen=True)
class Anchor:
    """Число, що рухається щодня. `relative=True` — поріг у відсотках
    від попереднього значення, інакше — в абсолютних одиницях."""

    value: float
    tolerance: float
    relative: bool = True


@dataclass(frozen=True)
class GateDecision:
    changed: bool
    reason: str
    since: Optional[datetime] = None


def fingerprint(stable) -> str:
    """Стабільний хеш входів: однакові дані → однаковий рядок незалежно
    від порядку ключів словника."""
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _moved(name: str, anchor: Anchor, previous: Optional[float]) -> Optional[str]:
    if previous is None:
        return f"{name}: нове число"
    if anchor.relative:
        if previous == 0:
            return None if anchor.value == 0 else f"{name}: зрушило з нуля"
        move = abs(anchor.value / previous - 1) * 100
        if move > anchor.tolerance:
            return f"{name}: {previous:g} → {anchor.value:g} ({move:.2f}% > {anchor.tolerance:g}%)"
        return None
    move = abs(anchor.value - previous)
    if move > anchor.tolerance:
        return f"{name}: {previous:g} → {anchor.value:g} (Δ {move:g} > {anchor.tolerance:g})"
    return None


def compare(
    previous: Optional[dict],
    stable,
    anchors: dict[str, Anchor],
    now: Optional[datetime] = None,
    max_age_days: Optional[int] = None,
) -> GateDecision:
    """Чисте рішення без БД. `previous` — {"fingerprint", "anchors",
    "updated_at"} минулого успішного аналізу або None."""
    if previous is None:
        return GateDecision(True, "минулого аналізу немає")

    now = now or datetime.now(timezone.utc)
    max_age = config.CHANGE_GATE_MAX_AGE_DAYS if max_age_days is None else max_age_days
    since = previous["updated_at"]
    if now - since >= timedelta(days=max_age):
        return GateDecision(True, f"висновку {(now - since).days} дн. — оновлюємо примусово", since)

    if previous["fingerprint"] != fingerprint(stable):
        return GateDecision(True, "з'явились нові дані", since)

    prev_anchors = previous.get("anchors") or {}
    for name, anchor in anchors.items():
        moved = _moved(name, anchor, prev_anchors.get(name))
        if moved:
            return GateDecision(True, moved, since)

    return GateDecision(False, "входи не змінились", since)


def check(conn, purpose: str, source_ref: str, stable, anchors: dict[str, Anchor]) -> GateDecision:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT fingerprint, anchors, updated_at
            FROM llm_input_fingerprints
            WHERE purpose = %s AND source_ref = %s
            """,
            (purpose, source_ref),
        )
        row = cur.fetchone()
    previous = (
        {"fingerprint": row[0], "anchors": row[1] or {}, "updated_at": row[2]} if row else None
    )
    return compare(previous, stable, anchors)


def remember(conn, purpose: str, source_ref: str, stable, anchors: dict[str, Anchor]) -> None:
    """Запам'ятати входи успішного аналізу — викликати ПІСЛЯ збереження
    висновку."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO llm_input_fingerprints (purpose, source_ref, fingerprint, anchors, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (purpose, source_ref) DO UPDATE SET
                fingerprint = EXCLUDED.fingerprint,
                anchors = EXCLUDED.anchors,
                updated_at = now()
            """,
            (
                purpose, source_ref, fingerprint(stable),
                json.dumps({name: a.value for name, a in anchors.items()}),
            ),
        )
    conn.commit()
