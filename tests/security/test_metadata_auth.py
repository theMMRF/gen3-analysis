import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from gen3analysis.metadata_auth import MetadataAuthMiddleware, request_access_token
from gen3analysis.gen3.guppyQuery import GuppyGQLClient


def protected_app():
    app = FastAPI()
    app.state.arborist_client = SimpleNamespace(
        auth_request=AsyncMock(return_value=True)
    )
    app.state.queries = 0

    @app.api_route("/cases/", methods=["GET", "POST"])
    @app.post("/genomic/gene_table")
    async def data():
        app.state.queries += 1
        await asyncio.sleep(0)
        return {"token": request_access_token.get()}

    @app.get("/_status")
    async def health():
        return {"status": "OK"}

    app.add_middleware(MetadataAuthMiddleware)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/cases/", "/genomic/gene_table", "/new-route", "/docs"]
)
async def test_anonymous_denied_before_query(path):
    app = protected_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(path)
    assert response.status_code == 401
    assert app.state.queries == 0
    app.state.arborist_client.auth_request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers, token",
    [
        ({"Authorization": "Bearer browse-only"}, "browse-only"),
        ({"Cookie": "access_token=browser-session"}, "browser-session"),
        (
            {"Authorization": "Bearer local-token", "Cookie": "access_token=other"},
            "local-token",
        ),
    ],
)
async def test_cookie_and_bearer_forwarded_after_authorization(headers, token):
    app = protected_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/cases/", headers=headers)
    assert response.status_code == 200
    assert response.json()["token"] == token
    app.state.arborist_client.auth_request.assert_awaited_once_with(
        token, "gen3-analysis", "read", ["/mmrf_metadata"]
    )
    assert request_access_token.get() is None


@pytest.mark.asyncio
async def test_bad_header_does_not_fall_back_to_cookie():
    app = protected_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/cases/",
            headers={"Authorization": "Basic bad", "Cookie": "access_token=valid"},
        )
    assert response.status_code == 401
    app.state.arborist_client.auth_request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome, status", [(False, 403), (None, 403), (RuntimeError("offline"), 503)]
)
async def test_denial_and_auth_service_failure_fail_closed(outcome, status):
    app = protected_app()
    auth = app.state.arborist_client.auth_request
    if isinstance(outcome, Exception):
        auth.side_effect = outcome
    else:
        auth.return_value = outcome
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/genomic/gene_table",
            headers={"Authorization": "Bearer expired-or-unapproved"},
        )
    assert response.status_code == status
    assert app.state.queries == 0


@pytest.mark.asyncio
async def test_concurrent_callers_are_isolated_and_health_remains_public():
    app = protected_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            *[
                client.post("/cases/", headers={"Authorization": f"Bearer user-{i}"})
                for i in range(10)
            ]
        )
        health = await client.get("/_status")
    assert [r.json()["token"] for r in responses] == [f"user-{i}" for i in range(10)]
    assert health.status_code == 200
    assert request_access_token.get() is None


@pytest.mark.asyncio
async def test_guppy_uses_validated_request_identity_not_cookie_argument():
    seen = []

    async def upstream(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"data": {}})

    guppy = GuppyGQLClient("http://guppy/graphql", "http://fence")
    guppy._http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    context = request_access_token.set("validated-local-bearer")
    try:
        await guppy.execute(access_token="stale-cookie", query="{cases {case_id}}")
    finally:
        request_access_token.reset(context)
        await guppy.close()
    assert seen == ["Bearer validated-local-bearer"]
