ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
COPY requirements-dev.txt /qualification/requirements-dev.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /qualification/requirements-dev.txt && python -m pip check
COPY tests/ /app/tests/
COPY scripts/qualify_processing.py scripts/qualify_framed_backup.py /app/scripts/
USER 10001:10001
