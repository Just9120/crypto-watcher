ARG BASE_IMAGE
FROM gcc:13.3.0-bookworm@sha256:1d71f0f3450214bef38fe09e6f610fb6cca90cf97b43f4ce845bfc32a4168818 AS builder
COPY scripts/processing_cost_probe.c /build/probe.c
RUN gcc -std=c11 -O2 -Wall -Wextra -Werror -fPIC -shared /build/probe.c -ldl -pthread -o /build/probe.so
FROM ${BASE_IMAGE}
ARG MEASUREMENT_REVISION
LABEL io.cryptowatcher.measurement=$MEASUREMENT_REVISION
COPY --from=builder /build/probe.so /diag/processing_cost_probe.so
COPY scripts/processing_cost_probe.py scripts/qualify_cpu_pair.py scripts/qualify_revision_pair.py scripts/cpu_pair_budget.py /app/scripts/
