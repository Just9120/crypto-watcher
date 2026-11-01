ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG MEASUREMENT_REVISION
LABEL io.cryptowatcher.measurement-revision=${MEASUREMENT_REVISION}
USER root
# Runtime and ALL scenario/tests are frozen in the base image. Only measurement
# control and the explicitly authorized CPU guard are overlaid, never app code.
COPY scripts/qualify_processing.py scripts/qualify_framed_backup.py scripts/cpu_pair_budget.py scripts/qualify_cpu_pair.py /app/scripts/
USER 10001:10001
