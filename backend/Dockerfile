FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

EXPOSE 8000
# docker-compose overrides this to run migrations first; the worker runs
# `python -m app.worker` from the same image.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
