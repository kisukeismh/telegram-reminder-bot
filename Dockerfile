FROM python:3.11-slim

WORKDIR /app

# Устанавливаем Poetry
RUN pip install --no-cache-dir poetry

# Копируем файлы зависимостей
COPY pyproject.toml poetry.lock ./

# Устанавливаем зависимости
RUN poetry config virtualenvs.create false && poetry install --no-interaction --no-ansi --only main

# Копируем код бота
COPY . .

# Запускаем бота
CMD ["python", "bot.py"]
