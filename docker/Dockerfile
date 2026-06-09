# -----------------------------------------------------------------------------
# zata-ops minimal CLI image.
# Use only when the host cannot install Python (e.g. ad-hoc container shells).
# Normal users should `uv tool install --force /path/to/zata-ops`.
# -----------------------------------------------------------------------------

FROM python:3.13-slim-bookworm

ARG POSTGRES_CLIENT_MAJOR=17

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
    && install -d /usr/share/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/postgresql.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-client-${POSTGRES_CLIENT_MAJOR} \
        sqlite3 \
        openssh-client \
        tar \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

ENTRYPOINT ["zata-ops"]
CMD ["--help"]
