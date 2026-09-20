# Build context is the REPOSITORY ROOT (see docker-compose.yml).
#
# RCA F2: with `context: ./backend`, nothing outside backend/ could be copied
# into the image -- in particular the vendored `pyquotex` broker library that
# lives at <repo>/vendor/. `PyQuotexProvider.connect()` then found no library
# and every real Quotex connection failed. The layout below puts the backend
# at /app/backend and the vendor tree at /app/vendor, and pins
# PYQUOTEX_VENDOR_PATH so app/pyquotex_vendor.py resolves it deterministically.
FROM python:3.12-slim

WORKDIR /app/backend

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      curl \
    && rm -rf /var/lib/apt/lists/*

# Core dependencies first (better layer caching), then the pyquotex extras.
# requirements-quotex.txt is NOT optional: pyquotex imports bs4 at module
# load, so skipping it turns "real broker" into a silent ModuleNotFoundError
# while the app still boots.
COPY backend/requirements.txt ./requirements.txt
COPY backend/requirements-quotex.txt ./requirements-quotex.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -r requirements-quotex.txt

COPY backend/ /app/backend/
COPY vendor/ /app/vendor/

# Deterministic vendor resolution (see backend/app/pyquotex_vendor.py and
# vendor/README.md). The resolver also works without this -- /app/vendor is an
# ancestor-search hit -- but pinning it makes the image layout explicit.
ENV PYQUOTEX_VENDOR_PATH=/app/vendor

EXPOSE 8090

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
