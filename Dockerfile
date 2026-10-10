FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

ARG SOURCE_REVISION=UNSET
ARG BUILD_RUN=UNSET
LABEL org.opencontainers.image.revision=$SOURCE_REVISION \
      io.cryptowatcher.build-run=$BUILD_RUN
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SOURCE_REVISION=$SOURCE_REVISION
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt && pip check \
    && groupadd --gid 10001 watcher \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin watcher \
    && mkdir /data && chown 10001:10001 /data
COPY cryptowatcher/ ./cryptowatcher/
COPY telegram_crypto_watcher.py ./
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "-m", "cryptowatcher", "health"]
CMD ["python", "-m", "cryptowatcher", "run"]
