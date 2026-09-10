"""Exercise the real router assembly, not just a toy protected endpoint."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from gen3analysis.main import get_app
from gen3analysis.gen3.guppyQuery import GuppyGQLClient
from gen3analysis.settings import settings


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/cases/", "/genomic/gene_table", "/survival", "/files/"]
)
async def test_real_data_routers_reject_anonymous(path):
    app = get_app()
    app.state.arborist_client = SimpleNamespace(
        auth_request=AsyncMock(return_value=True)
    )
    app.state.guppy_client = SimpleNamespace(execute=AsyncMock())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(path, json={})
    assert response.status_code == 401
    app.state.guppy_client.execute.assert_not_called()
    app.state.arborist_client.auth_request.assert_not_called()


@pytest.mark.asyncio
async def test_real_cases_route_forwards_local_bearer_through_guppy():
    seen = []

    async def upstream(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(
            200,
            json={
                "data": {
                    settings.case_centric_gql: [{"case_id": "test-case"}],
                    settings.case_centric_agg_gql: {
                        settings.CASE_CENTRIC_INDEX: {"_totalCount": 1}
                    },
                }
            },
        )

    app = get_app()
    app.state.arborist_client = SimpleNamespace(
        auth_request=AsyncMock(return_value=True)
    )
    guppy = GuppyGQLClient("http://guppy/graphql", "http://fence")
    guppy._http_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    app.state.guppy_client = guppy
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/cases/",
                headers={"Authorization": "Bearer local-browse-only"},
                json={"fields": ["case_id"], "size": 1},
            )
        assert response.status_code == 200
        assert response.json()["data"] == [{"case_id": "test-case"}]
        assert response.headers["cache-control"] == "private, no-store"
        assert seen == ["Bearer local-browse-only"]
    finally:
        await guppy.close()
