import json
import ssl
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus

import boto3
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from starlette import status


@dataclass(frozen=True)
class TermsDatabaseConfig:
    enabled: bool
    database_url: Optional[str]
    host: Optional[str]
    port: int
    database: Optional[str]
    user: Optional[str]
    secret_arn: Optional[str]
    ssl_mode: Optional[str]
    ssl_root_cert: Optional[str]


def get_terms_database_config(settings) -> TermsDatabaseConfig:
    return TermsDatabaseConfig(
        enabled=settings.TERMS_DB_ENABLED,
        database_url=settings.TERMS_ACCEPTANCE_DATABASE_URL,
        host=settings.TERMS_DB_HOST,
        port=settings.TERMS_DB_PORT,
        database=settings.TERMS_DB_NAME,
        user=settings.TERMS_DB_USER,
        secret_arn=settings.TERMS_DB_SECRET_ARN,
        ssl_mode=settings.TERMS_DB_SSL_MODE,
        ssl_root_cert=settings.TERMS_DB_SSL_ROOT_CERT,
    )


def get_terms_database_password(secret_arn: str) -> str:
    arn_parts = secret_arn.split(":")
    region_name = arn_parts[3] if len(arn_parts) > 3 and arn_parts[3] else None
    client = boto3.client("secretsmanager", region_name=region_name)
    response = client.get_secret_value(SecretId=secret_arn)
    secret_string = response.get("SecretString")
    if not secret_string:
        raise ValueError("Terms database secret does not contain SecretString")

    secret = json.loads(secret_string)
    password = secret.get("password")
    if not password:
        raise ValueError("Terms database secret does not contain a password")

    return password


def build_terms_database_url(config: TermsDatabaseConfig, password: str) -> str:
    if not config.host or not config.database or not config.user:
        raise ValueError("Terms database host, name, and user must be configured")

    return (
        "postgresql+asyncpg://"
        f"{quote_plus(config.user)}:{quote_plus(password)}@"
        f"{config.host}:{config.port}/{quote_plus(config.database)}"
    )


def build_terms_database_ssl_context(
    ssl_mode: Optional[str],
    ssl_root_cert: Optional[str],
) -> Optional[ssl.SSLContext]:
    if not ssl_mode or ssl_mode in {"disable", "allow", "prefer"}:
        return None

    if ssl_mode == "verify-full":
        if not ssl_root_cert:
            raise ValueError("TERMS_DB_SSL_ROOT_CERT is required for verify-full")
        return ssl.create_default_context(cafile=ssl_root_cert)

    if ssl_mode == "require":
        return ssl.create_default_context()

    raise ValueError(f"Unsupported TERMS_DB_SSL_MODE: {ssl_mode}")


def create_terms_acceptance_engine(config: TermsDatabaseConfig) -> Optional[AsyncEngine]:
    if not config.enabled and not config.database_url:
        return None

    database_url = config.database_url
    use_terms_db_ssl_config = False
    if not database_url:
        if not config.secret_arn:
            raise ValueError("TERMS_DB_SECRET_ARN is required when terms DB is enabled")
        password = get_terms_database_password(config.secret_arn)
        database_url = build_terms_database_url(config, password)
        use_terms_db_ssl_config = True

    ssl_context = None
    if use_terms_db_ssl_config:
        ssl_context = build_terms_database_ssl_context(
            config.ssl_mode,
            config.ssl_root_cert,
        )
    connect_args = {"ssl": ssl_context} if ssl_context is not None else {}

    return create_async_engine(
        database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )


def create_terms_acceptance_sessionmaker(
    engine: Optional[AsyncEngine],
) -> Optional[async_sessionmaker]:
    if engine is None:
        return None

    return async_sessionmaker(engine, expire_on_commit=False)


def get_terms_acceptance_sessionmaker(request: Request) -> async_sessionmaker:
    sessionmaker = getattr(request.app.state, "terms_acceptance_sessionmaker", None)
    if sessionmaker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Terms acceptance database is not configured",
        )

    return sessionmaker
