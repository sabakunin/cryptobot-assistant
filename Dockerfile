FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY sniper ./sniper
COPY migrations ./migrations
COPY alembic.ini .env.example main.py ./
RUN useradd --create-home --uid 10001 assistant && mkdir -p /data && chown assistant:assistant /data
USER assistant
EXPOSE 8000
CMD ["python", "main.py"]

