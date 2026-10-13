FROM python:3.12-slim

# Без цього stdout буферизується при не-TTY виводі (docker compose exec
# без -it, docker compose logs) — короткі скрипти (напр.
# orchestration/run_job.py) можуть губити останні рядки виводу при
# виході процесу. logging (StreamHandler) сам робить flush після
# кожного запису й це не зачіпає — але plain print() ні.
ENV PYTHONUNBUFFERED=1

# Корінь репозиторію в PYTHONPATH — щоб ЄДИНИЙ `config.py` (рішення
# користувача 2026-10-04: один конфіг на весь агент) імпортувався з
# будь-якої точки входу: і зі скриптів, і з callable-джоб
# (orchestration/jobs.py), і з `docker compose exec`.
#
# ЖИВИЙ ЗБІЙ 2026-10-05, через який це тут: спершу я додав корінь у
# `sys.path` ВРУЧНУ в 7 скриптів, які сам і перевірив — а джоб, що
# транзитивно тягнуть конфіг, значно більше (consolidate.py,
# merge_similar.py, synthesize_market.py, callable _watchlist_prices).
# Уночі провалилось 7 джоб із ModuleNotFoundError: No module named
# 'config'. Поскриптовий sys.path — ненадійний спосіб: він покриває
# лише ті входи, про які згадали.
ENV PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sleep", "infinity"]
