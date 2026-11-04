ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG MEASUREMENT_REVISION
ARG SUBJECT_SERVICES
LABEL io.cryptowatcher.measurement-revision=${MEASUREMENT_REVISION}
LABEL io.cryptowatcher.subject-services="canonical-services-v1"
ENV CW_SUBJECT_SERVICES=${SUBJECT_SERVICES}
USER root
COPY scripts/qualify_cpu_pair.py scripts/cpu_pair_budget.py scripts/qualify_group_pair.py scripts/qualify_service_pair.py scripts/qualify_processing.py scripts/qualify_service_preflight.py /app/scripts/
COPY tests/processing_replay.py tests/processing_observation.py /app/tests/
USER 10001:10001
