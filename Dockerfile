# The bank and the merchant as one image (two programs): non-root user, hash-pinned dependencies, the health command
# as its check. The default command is the bank; run the merchant with `open-banking-merchant`.
#   docker build -t payments-secure-open-banking:dev .
#   docker compose --profile app up --build        (PostgreSQL and the bank together)
FROM python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BANK_HOST=0.0.0.0 \
    BANK_PORT=8463

WORKDIR /app
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

RUN useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /data && chown app /data
USER app
VOLUME /data

EXPOSE 8463
HEALTHCHECK --interval=15s --timeout=6s --retries=3 CMD ["open-banking-health"]
CMD ["open-banking-bank"]
