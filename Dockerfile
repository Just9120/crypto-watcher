FROM gcc:13.3.0-bookworm@sha256:1d71f0f3450214bef38fe09e6f610fb6cca90cf97b43f4ce845bfc32a4168818 AS native-builder
WORKDIR /build
COPY native/ ./native/
RUN cd native/vendor/yyjson \
    && echo '175867c5493a5df648cec566717fa1c29aa2f6096f5f0cf1efad0b65e1f6d7b3  yyjson.h' | sha256sum -c - \
    && echo 'ac2e9bbb2e2d9149d90878d40506a1d624fa0b33c979a11b61075c54782c6d6a  yyjson.c' | sha256sum -c - \
    && gcc -std=c99 -O3 -fPIC -shared -I. yyjson.c ../../history_extract.c -lm -o /build/history.so
RUN bash native/build_vfs.sh /build/compressed_vfs.so

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
COPY --from=native-builder /build/history.so ./cryptowatcher/_native/history.so
COPY --from=native-builder /build/compressed_vfs.so ./cryptowatcher/_native/compressed_vfs.so
COPY --from=native-builder /build/native/vendor/yyjson/LICENSE ./cryptowatcher/_native/LICENSE.yyjson
COPY --from=native-builder /build/native/vendor/zstd/LICENSE ./cryptowatcher/_native/LICENSE.zstd
COPY --from=native-builder /build/native/vendor/sqlite/LICENSE ./cryptowatcher/_native/LICENSE.sqlite
COPY telegram_crypto_watcher.py ./
COPY scripts/observe_capacity.py ./operator/observe_capacity.py
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "-m", "cryptowatcher", "health"]
CMD ["python", "-m", "cryptowatcher", "run"]
