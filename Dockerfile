FROM gcc:13.3.0-bookworm@sha256:1d71f0f3450214bef38fe09e6f610fb6cca90cf97b43f4ce845bfc32a4168818 AS native-builder
WORKDIR /build
COPY native/ ./native/
RUN cd native/vendor/yyjson \
    && echo '175a74d7013149c736d2e59d6fd372e01d360dde9b583ba9063ac8037f4dbf8b  yyjson.h' | sha256sum -c - \
    && echo '5b368f298311b1f3f107e685bddf218d607dee6227c1340405974040f7c7e4ca  yyjson.c' | sha256sum -c - \
    && gcc -std=c99 -O3 -fPIC -shared -I. yyjson.c ../../history_extract.c -lm -o /build/history.so
RUN bash native/build_vfs.sh /build/compressed_vfs.so
RUN bash native/build_numeric.sh /build/numeric_frame.so
RUN bash native/build_source_guard.sh /build/source_guard.so

FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

ARG SOURCE_REVISION=UNSET
ARG BUILD_RUN=UNSET
LABEL org.opencontainers.image.revision=$SOURCE_REVISION \
      io.cryptowatcher.build-run=$BUILD_RUN
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 SOURCE_REVISION=$SOURCE_REVISION
WORKDIR /app
COPY requirements.txt requirements-bootstrap.txt ./
RUN python -m pip install --no-cache-dir --upgrade --require-hashes --only-binary=:all: -r requirements-bootstrap.txt \
    && python -m pip install --no-cache-dir --require-hashes -r requirements.txt && python -m pip check \
    && groupadd --gid 10001 watcher \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin watcher \
    && mkdir /data && chown 10001:10001 /data
COPY cryptowatcher/ ./cryptowatcher/
COPY --from=native-builder /build/history.so ./cryptowatcher/_native/history.so
COPY --from=native-builder /build/compressed_vfs.so ./cryptowatcher/_native/compressed_vfs.so
COPY --from=native-builder /build/numeric_frame.so ./cryptowatcher/_native/numeric_frame.so
COPY --from=native-builder /build/source_guard.so ./cryptowatcher/_native/source_guard.so
COPY --from=native-builder /build/native/vendor/yyjson/LICENSE ./cryptowatcher/_native/LICENSE.yyjson
COPY --from=native-builder /build/native/vendor/zstd/LICENSE ./cryptowatcher/_native/LICENSE.zstd
COPY --from=native-builder /build/native/vendor/sqlite/LICENSE ./cryptowatcher/_native/LICENSE.sqlite
COPY telegram_crypto_watcher.py ./
COPY scripts/observe_capacity.py ./operator/observe_capacity.py
COPY scripts/fresh_measurement.py ./operator/fresh_measurement.py
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "-m", "cryptowatcher", "health"]
CMD ["python", "-m", "cryptowatcher", "run"]
