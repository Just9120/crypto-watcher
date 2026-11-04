ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG MEASUREMENT_REVISION
ARG COUNTER_DELTA
ARG APPLICATION_REVISION
LABEL io.cryptowatcher.measurement-revision=${MEASUREMENT_REVISION}
LABEL io.cryptowatcher.counter-delta="temp-sql-ledger-v1"
LABEL org.opencontainers.image.revision=${APPLICATION_REVISION}
ENV CW_COUNTER_DELTA=${COUNTER_DELTA} TMPDIR=/work SOURCE_REVISION=${APPLICATION_REVISION}
USER root
COPY scripts/qualify_cpu_pair.py scripts/cpu_pair_budget.py scripts/qualify_counter_pair.py /app/scripts/
COPY scripts/counter_supervisor.py /app/scripts/qualify_group_pair.py
USER 10001:10001
