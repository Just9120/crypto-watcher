ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
COPY tests/domain_writer.py tests/domain_roles.py tests/processing_replay.py /app/tests/
ENV CW_ISOLATED_DOMAIN_WRITER=typed-domain-v1
LABEL io.cryptowatcher.domain-writer=typed-domain-v1
USER 10001:10001
