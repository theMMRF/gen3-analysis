"""Validate real signed tokens through Auth and the locked authutils library."""

import asyncio
import time
from types import SimpleNamespace

import jwt
import pytest
from authutils.token import fastapi as token_validator
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from starlette.requests import Request

from gen3analysis.auth import Auth
from gen3analysis.settings import CoreSettings, settings

ISSUER = "https://fence.example.test/user"


@pytest.fixture(scope="module")
def signing_keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public


async def validate(monkeypatch, signing_keys, claims=None, cookie=False, bad_key=False):
    private, public = signing_keys
    keys = asyncio.get_running_loop().create_future()
    keys.set_result({"test-key": public})
    monkeypatch.setattr(token_validator, "_jwt_public_keys", {ISSUER: keys})
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": "123",
        "aud": ["gen3"],
        "scope": ["openid", "user"],
        "pur": "access",
        "iat": now,
        "exp": now + 300,
    }
    payload.update(claims or {})
    if bad_key:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(payload, private, algorithm="RS256", headers={"kid": "test-key"})
    request = Request(
        {
            "type": "http",
            "headers": (
                [(b"cookie", f"access_token={token}".encode())] if cookie else []
            ),
            "app": SimpleNamespace(state=SimpleNamespace(arborist_client=None)),
        }
    )
    bearer = (
        None
        if cookie
        else HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    )
    return await Auth(request, bearer_token=bearer).get_token_claims()


@pytest.mark.asyncio
@pytest.mark.parametrize("cookie", [False, True])
async def test_new_fence_token_with_configured_audience(
    monkeypatch, signing_keys, cookie
):
    monkeypatch.setattr(settings, "ACCESS_TOKEN_AUDIENCE", "gen3")
    claims = await validate(monkeypatch, signing_keys, cookie=cookie)
    assert claims["sub"] == "123"


@pytest.mark.asyncio
async def test_legacy_default_preserves_existing_fence(monkeypatch, signing_keys):
    assert CoreSettings().ACCESS_TOKEN_AUDIENCE == "openid"
    monkeypatch.setattr(settings, "ACCESS_TOKEN_AUDIENCE", "openid")
    claims = await validate(
        monkeypatch, signing_keys, {"aud": [ISSUER, "openid", "user"]}
    )
    assert claims["sub"] == "123"
    # This is the exact pre-fix mismatch observed in dev.
    with pytest.raises(HTTPException) as error:
        await validate(monkeypatch, signing_keys)
    assert error.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims,bad_key",
    [
        ({"aud": ["other-service"]}, False),
        ({"aud": ["openid"]}, False),
        ({"aud": []}, False),
        ({"scope": ["openid"]}, False),
        ({"scope": ["user"]}, False),
        ({"pur": "id"}, False),
        ({"exp": 1}, False),
        ({}, True),
    ],
)
async def test_validation_stays_enforced(monkeypatch, signing_keys, claims, bad_key):
    monkeypatch.setattr(settings, "ACCESS_TOKEN_AUDIENCE", "gen3")
    with pytest.raises(HTTPException) as error:
        await validate(monkeypatch, signing_keys, claims, bad_key=bad_key)
    assert error.value.status_code == 401


def test_empty_audience_rejected():
    with pytest.raises(ValidationError):
        CoreSettings(ACCESS_TOKEN_AUDIENCE="")
