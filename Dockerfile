ARG AZLINUX_BASE_VERSION=master
FROM quay.io/cdis/python-nginx-al:${AZLINUX_BASE_VERSION} AS base

ENV appname=gen3analysis

WORKDIR /${appname}

RUN chown -R gen3:gen3 /${appname}

# Builder stage
FROM base AS builder

USER gen3

# copy ONLY poetry artifact, install the dependencies but not the app;
# this will make sure that the dependencies are cached
COPY poetry.lock pyproject.toml /${appname}/
RUN poetry install -vv --no-root --only main --no-interaction

COPY --chown=gen3:gen3 . /${appname}

# install the app
RUN poetry install --without dev --no-interaction

# Final stage
FROM base

COPY --from=builder /${appname} /${appname}

# curl-minimal is already in the base image; do not install the curl package.
RUN dnf -y install vim \
    && curl -fsSL -o /etc/ssl/certs/rds-global-bundle.pem \
        https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
    && chmod 644 /etc/ssl/certs/rds-global-bundle.pem

ENV GENE_EXPRESSION_SQLITE_PATH=/opt/mmrf_gene_expression_test_data/schemaless.sqlite3
ENV GENE_EXPRESSION_DATA_DIR=/opt/mmrf_gene_expression_test_data/mmrf_test_data
ENV GENE_EXPRESSION_ENABLED=true

# Switch to non-root user 'gen3' for the serving process
USER gen3

WORKDIR /${appname}

RUN chmod 755 bin/run.sh

CMD ["bash", "bin/run.sh"]
