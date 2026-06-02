# Build stage
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Создаем виртуальное окружение
RUN python -m venv /opt/venv
# Активируем его для установки зависимостей
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
# Убираем флаг --user, так как ставим в venv
RUN pip install --no-cache-dir -r requirements.txt

# Runtime stage
FROM python:3.11-slim

WORKDIR /app

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

# Копируем виртуальное окружение из builder
COPY --from=builder /opt/venv /opt/venv
# Добавляем bin из venv в PATH
ENV PATH="/opt/venv/bin:$PATH"

# Copy application code
COPY . .

# Ensure proper permissions
RUN chown -R appuser:appuser /app

USER appuser

# Меняем порт на 8081, чтобы он совпадал с docker-compose и nginx
EXPOSE 8081

# Запускаем gunicorn на порту 8081
CMD ["gunicorn", "--bind", "0.0.0.0:8081", "--workers", "2", "--timeout", "120", "main:app"]