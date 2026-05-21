from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from starlette import status


def create_terms_acceptance_engine(database_url: Optional[str]) -> Optional[AsyncEngine]:
    if not database_url:
        return None

    return create_async_engine(database_url, pool_pre_ping=True)


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
