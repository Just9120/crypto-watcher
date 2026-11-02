ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG MEASUREMENT_REVISION
LABEL io.cryptowatcher.measurement-revision=${MEASUREMENT_REVISION}
USER root
# Overlay the SAME measurement code only. Application and scenario stay frozen.
COPY scripts/qualify_revision_pair.py scripts/qualify_cpu_pair.py scripts/cpu_pair_budget.py /app/scripts/
USER 10001:10001
