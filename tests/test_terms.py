from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from gen3analysis.auth import Auth
from gen3analysis.routes.terms import get_terms_acceptance_sessionmaker
from gen3analysis.terms_acceptance.service import user_from_claims


CURRENT_TERMS = SimpleNamespace(
    id=1,
    version="2026-05-21",
    effective_at=datetime(2026, 5, 21, tzinfo=timezone.utc),
    terms_url="https://example.org/terms/2026-05-21",
    terms_content="<h1>Terms</h1>",
    content_format="html",
)


class FakeResult:
    def __init__(self, row=None, scalar=None, rowcount=0):
        self._row = row
        self._scalar = scalar
        self.rowcount = rowcount

    def one_or_none(self):
        return self._row

    def scalar_one(self):
        return self._scalar


class FakeSession:
    def __init__(self, accepted=False, inserted=True):
        self.accepted = accepted
        self.inserted = inserted
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, statement, params=None):
        statement_text = str(statement)
        if "from terms_versions" in statement_text:
            return FakeResult(row=CURRENT_TERMS)
        if "select exists" in statement_text:
            return FakeResult(scalar=self.accepted)
        if "insert into terms_acceptances" in statement_text:
            return FakeResult(rowcount=1 if self.inserted else 0)
        raise AssertionError(f"Unexpected SQL: {statement_text}")

    async def commit(self):
        self.committed = True


class FakeSessionMaker:
    def __init__(self, accepted=False, inserted=True):
        self.accepted = accepted
        self.inserted = inserted

    def __call__(self):
        return FakeSession(accepted=self.accepted, inserted=self.inserted)


class FakeAuth:
    def __init__(self, claims=None):
        self.claims = claims or {
            "sub": "user-1",
            "email": "user@example.org",
            "name": "Test User",
        }

    async def get_token_claims(self):
        return self.claims


@pytest.fixture(autouse=True)
def clear_dependency_overrides(app):
    app.dependency_overrides = {}
    yield
    app.dependency_overrides = {}


def override_terms_sessionmaker(accepted=False, inserted=True):
    return lambda: FakeSessionMaker(accepted=accepted, inserted=inserted)


def test_user_from_claims_prefers_sub_and_email():
    user = user_from_claims(
        {"sub": "sub-1", "email": "user@example.org", "name": "Example User"}
    )

    assert user.user_id == "sub-1"
    assert user.email == "user@example.org"
    assert user.name == "Example User"


def test_user_from_claims_falls_back_to_context_user():
    user = user_from_claims(
        {"context": {"user": {"email": "nested@example.org", "name": "Nested User"}}}
    )

    assert user.user_id == "nested@example.org"
    assert user.email == "nested@example.org"
    assert user.name == "Nested User"


def test_user_from_claims_requires_email():
    with pytest.raises(HTTPException) as exc_info:
        user_from_claims({"sub": "sub-1"})

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_current_terms_returns_current_version(app, client):
    app.dependency_overrides[
        get_terms_acceptance_sessionmaker
    ] = override_terms_sessionmaker()

    response = await client.get("/terms/current")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 1
    assert body["version"] == "2026-05-21"
    assert body["terms_content"] == "<h1>Terms</h1>"
    assert body["content_format"] == "html"


@pytest.mark.asyncio
async def test_terms_status_returns_unaccepted_state(app, client):
    app.dependency_overrides[
        get_terms_acceptance_sessionmaker
    ] = override_terms_sessionmaker(accepted=False)
    app.dependency_overrides[Auth] = lambda: FakeAuth()

    response = await client.get("/terms/status")

    assert response.status_code == 200
    body = response.json()
    assert body["has_accepted_latest_terms"] is False
    assert body["current_terms"]["id"] == 1


@pytest.mark.asyncio
async def test_accept_terms_records_current_version(app, client):
    app.dependency_overrides[
        get_terms_acceptance_sessionmaker
    ] = override_terms_sessionmaker(inserted=True)
    app.dependency_overrides[Auth] = lambda: FakeAuth()

    response = await client.post("/terms/acceptances", json={"terms_version_id": 1})

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "has_accepted_latest_terms": True,
        "terms_version_id": 1,
        "accepted": True,
    }


@pytest.mark.asyncio
async def test_accept_terms_is_idempotent(app, client):
    app.dependency_overrides[
        get_terms_acceptance_sessionmaker
    ] = override_terms_sessionmaker(inserted=False)
    app.dependency_overrides[Auth] = lambda: FakeAuth()

    response = await client.post("/terms/acceptances", json={"terms_version_id": 1})

    assert response.status_code == 200
    assert response.json()["accepted"] is False


@pytest.mark.asyncio
async def test_accept_terms_rejects_stale_version(app, client):
    app.dependency_overrides[
        get_terms_acceptance_sessionmaker
    ] = override_terms_sessionmaker()
    app.dependency_overrides[Auth] = lambda: FakeAuth()

    response = await client.post("/terms/acceptances", json={"terms_version_id": 999})

    assert response.status_code == 409
