import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from gen3analysis.terms_acceptance.database import (
    TermsDatabaseConfig,
    build_terms_database_ssl_context,
    build_terms_database_url,
    get_terms_database_config,
    get_terms_database_password,
)


def test_get_terms_database_config_maps_settings():
    settings = SimpleNamespace(
        TERMS_DB_ENABLED=True,
        TERMS_ACCEPTANCE_DATABASE_URL=None,
        TERMS_DB_HOST="example.rds.amazonaws.com",
        TERMS_DB_PORT=5432,
        TERMS_DB_NAME="terms_acceptance",
        TERMS_DB_USER="postgres",
        TERMS_DB_SECRET_ARN="arn:aws:secretsmanager:region:acct:secret:name",
        TERMS_DB_SSL_MODE="verify-full",
        TERMS_DB_SSL_ROOT_CERT="/etc/ssl/certs/rds-global-bundle.pem",
    )

    config = get_terms_database_config(settings)

    assert config.enabled is True
    assert config.host == "example.rds.amazonaws.com"
    assert config.secret_arn == "arn:aws:secretsmanager:region:acct:secret:name"


def test_build_terms_database_url_url_encodes_credentials():
    config = TermsDatabaseConfig(
        enabled=True,
        database_url=None,
        host="example.rds.amazonaws.com",
        port=5432,
        database="terms_acceptance",
        user="terms user",
        secret_arn="secret",
        ssl_mode="verify-full",
        ssl_root_cert="/cert.pem",
    )

    url = build_terms_database_url(config, "p@ss/word")
    expected_url = (
        "postgresql+asyncpg://terms+user:p%40ss%2Fword@"
        "example.rds.amazonaws.com:5432/terms_acceptance"
    )

    assert url == expected_url


def test_get_terms_database_password_reads_secret_string_password():
    client = Mock()
    client.get_secret_value.return_value = {
        "SecretString": json.dumps({"password": "secret-password"})
    }

    with patch(
        "gen3analysis.terms_acceptance.database.boto3.client",
        return_value=client,
    ):
        password = get_terms_database_password("secret-arn")

    assert password == "secret-password"
    client.get_secret_value.assert_called_once_with(SecretId="secret-arn")


def test_get_terms_database_password_uses_region_from_secret_arn():
    client = Mock()
    client.get_secret_value.return_value = {
        "SecretString": json.dumps({"password": "secret-password"})
    }
    secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-secret"

    with patch(
        "gen3analysis.terms_acceptance.database.boto3.client",
        return_value=client,
    ) as boto_client:
        get_terms_database_password(secret_arn)

    boto_client.assert_called_once_with("secretsmanager", region_name="us-east-1")


def test_build_terms_database_ssl_context_requires_root_cert_for_verify_full():
    with pytest.raises(ValueError):
        build_terms_database_ssl_context("verify-full", None)


def test_build_terms_database_ssl_context_returns_none_when_disabled():
    assert build_terms_database_ssl_context("disable", None) is None
