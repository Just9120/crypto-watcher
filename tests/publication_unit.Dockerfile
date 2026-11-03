ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
COPY tests/publication_unit.py /app/tests/
ENV CW_ISOLATED_PUBLICATION_UNIT=one-pass-v1
LABEL io.cryptowatcher.publication-unit=one-pass-v1
USER 10001:10001
