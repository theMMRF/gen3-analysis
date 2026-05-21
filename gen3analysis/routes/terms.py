from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette import status

from gen3analysis.auth import Auth
from gen3analysis.models.terms import (
    TermsAcceptanceRequest,
    TermsAcceptanceResponse,
    TermsStatusResponse,
    TermsVersionResponse,
)
from gen3analysis.terms_acceptance.database import get_terms_acceptance_sessionmaker
from gen3analysis.terms_acceptance.service import (
    TermsVersion,
    accept_current_terms,
    get_current_terms_version,
    has_accepted_latest_terms,
    user_from_claims,
)

terms = APIRouter()


def serialize_terms_version(terms_version: TermsVersion) -> TermsVersionResponse:
    return TermsVersionResponse(
        id=terms_version.id,
        version=terms_version.version,
        effective_at=terms_version.effective_at,
        terms_url=terms_version.terms_url,
        terms_content=terms_version.terms_content,
        content_format=terms_version.content_format,
    )


@terms.get(
    "/current",
    status_code=status.HTTP_200_OK,
    response_model=TermsVersionResponse,
    summary="Get current Terms & Conditions",
)
async def current_terms(
    sessionmaker: async_sessionmaker = Depends(get_terms_acceptance_sessionmaker),
) -> TermsVersionResponse:
    async with sessionmaker() as session:
        terms_version = await get_current_terms_version(session)
    return serialize_terms_version(terms_version)


@terms.get(
    "/status",
    status_code=status.HTTP_200_OK,
    response_model=TermsStatusResponse,
    summary="Check whether the authenticated user accepted the current terms",
)
async def terms_status(
    auth: Auth = Depends(Auth),
    sessionmaker: async_sessionmaker = Depends(get_terms_acceptance_sessionmaker),
) -> TermsStatusResponse:
    claims = await auth.get_token_claims()
    user = user_from_claims(claims)

    async with sessionmaker() as session:
        terms_version = await get_current_terms_version(session)
        accepted = await has_accepted_latest_terms(session, user.user_id)

    return TermsStatusResponse(
        has_accepted_latest_terms=accepted,
        current_terms=serialize_terms_version(terms_version),
    )


@terms.post(
    "/acceptances",
    status_code=status.HTTP_200_OK,
    response_model=TermsAcceptanceResponse,
    summary="Accept the current Terms & Conditions",
)
async def accept_terms(
    body: TermsAcceptanceRequest,
    auth: Auth = Depends(Auth),
    sessionmaker: async_sessionmaker = Depends(get_terms_acceptance_sessionmaker),
) -> TermsAcceptanceResponse:
    claims = await auth.get_token_claims()
    user = user_from_claims(claims)

    async with sessionmaker() as session:
        inserted = await accept_current_terms(session, user, body.terms_version_id)

    return TermsAcceptanceResponse(
        has_accepted_latest_terms=True,
        terms_version_id=body.terms_version_id,
        accepted=inserted,
    )
