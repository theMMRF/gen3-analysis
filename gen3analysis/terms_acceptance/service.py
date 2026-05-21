from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status


@dataclass(frozen=True)
class TermsUser:
    user_id: str
    email: str
    name: Optional[str] = None


@dataclass(frozen=True)
class TermsVersion:
    id: int
    version: str
    effective_at: datetime
    terms_url: Optional[str]
    terms_content: str
    content_format: str


def _nested_get(data: Mapping[str, Any], path: tuple[str, ...]) -> Optional[Any]:
    current: Any = data
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def user_from_claims(claims: Mapping[str, Any]) -> TermsUser:
    email = (
        claims.get("email")
        or _nested_get(claims, ("context", "user", "email"))
        or claims.get("preferred_username")
    )
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user email is required to accept terms",
        )

    user_id = str(claims.get("sub") or email)
    name = claims.get("name") or _nested_get(claims, ("context", "user", "name"))
    return TermsUser(user_id=user_id, email=str(email), name=name)


def _version_from_row(row: Any) -> TermsVersion:
    return TermsVersion(
        id=row.id,
        version=row.version,
        effective_at=row.effective_at,
        terms_url=row.terms_url,
        terms_content=row.terms_content,
        content_format=row.content_format,
    )


async def get_current_terms_version(session: AsyncSession) -> TermsVersion:
    result = await session.execute(
        text(
            """
            select id, version, effective_at, terms_url, terms_content, content_format
            from terms_versions
            where is_current = true
            """
        )
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Current terms version is not configured",
        )

    return _version_from_row(row)


async def has_accepted_latest_terms(
    session: AsyncSession,
    user_id: str,
) -> bool:
    result = await session.execute(
        text(
            """
            select exists (
              select 1
              from terms_acceptances ta
              join terms_versions tv on tv.id = ta.terms_version_id
              where ta.user_id = :user_id
                and tv.is_current = true
            ) as has_accepted_latest_terms
            """
        ),
        {"user_id": user_id},
    )
    return bool(result.scalar_one())


async def accept_current_terms(
    session: AsyncSession,
    user: TermsUser,
    terms_version_id: int,
) -> bool:
    current_terms = await get_current_terms_version(session)
    if terms_version_id != current_terms.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Submitted terms version is no longer current",
        )

    result = await session.execute(
        text(
            """
            insert into terms_acceptances (
                user_id,
                email,
                name,
                terms_version_id
            ) values (
                :user_id,
                :email,
                :name,
                :terms_version_id
            )
            on conflict (user_id, terms_version_id) do nothing
            """
        ),
        {
            "user_id": user.user_id,
            "email": user.email,
            "name": user.name,
            "terms_version_id": terms_version_id,
        },
    )
    await session.commit()
    return result.rowcount > 0
