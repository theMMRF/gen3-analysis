from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class TermsVersionResponse(BaseModel):
    id: int
    version: str
    effective_at: datetime
    terms_url: Optional[str] = None
    terms_content: str
    content_format: str


class TermsStatusResponse(BaseModel):
    has_accepted_latest_terms: bool
    current_terms: TermsVersionResponse


class TermsAcceptanceRequest(BaseModel):
    terms_version_id: int = Field(
        ..., description="The current terms version ID displayed to the user"
    )


class TermsAcceptanceResponse(BaseModel):
    has_accepted_latest_terms: bool
    terms_version_id: int
    accepted: bool
