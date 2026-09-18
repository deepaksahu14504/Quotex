FROM python:3.12-slim

WORKDIR /app

# libpq is needed at runtime by psycopg (even the [binary] wheel needs the
# client shared library on some base images); gcc only needed if a wheel
# isn't available for this platform and psycopg falls back to source build.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8090

# db.init_db() (called from FastAPI's lifespan handler in app/main.py) runs
# `alembic upgrade head` on startup, so migrations apply automatically on
# every deploy -- no separate migration step needed here.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
