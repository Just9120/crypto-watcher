ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG MEASUREMENT_REVISION
LABEL io.cryptowatcher.measurement-revision=${MEASUREMENT_REVISION}
LABEL io.cryptowatcher.group-publication="clean-pair-v1"
USER root
COPY scripts/qualify_cpu_pair.py scripts/cpu_pair_budget.py scripts/qualify_group_pair.py /app/scripts/
COPY tests/processing_replay.py tests/processing_observation.py /app/tests/
USER 10001:10001
