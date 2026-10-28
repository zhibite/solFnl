FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash appuser

COPY --from=builder /install /usr/local
COPY app/ .

# NOTE: intentionally run as root. The /data directory is provided by a
# bind-mount / named volume whose host-side ownership is unknown
# (e.g. Coolify creates /gmgndaily/data as root). Running as a fixed
# non-root user (appuser) breaks SQLite writes with
# "sqlite3.OperationalError: unable to open database file".
# For a single-file self-hosted SQLite app, root-in-container is the
# pragmatic choice; the container still only exposes the app port.
RUN mkdir -p /data

EXPOSE 8038

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

CMD ["python", "app.py"]
