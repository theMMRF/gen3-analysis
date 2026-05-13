import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gen3analysis.gen3.guppyQuery import GuppyGQLClient
from gen3analysis.settings import settings


def test_timeout_defaults():
    assert settings.GUPPY_HTTP_TIMEOUT == 180
    assert settings.ES_TIMEOUT == 180
    assert settings.GUNICORN_TIMEOUT == 210
    assert settings.GUNICORN_GRACEFUL_TIMEOUT == 210


def test_guppy_shared_client_uses_configured_timeout():
    client = GuppyGQLClient(
        graphql_url="http://guppy-service/graphql",
        csrf_token_url="http://revproxy-service",
    )

    with patch("gen3analysis.gen3.guppyQuery.httpx.AsyncClient") as mock_async_client:
        mock_async_client.return_value.is_closed = False

        client._get_client()

    assert mock_async_client.call_args.kwargs["timeout"] == settings.GUPPY_HTTP_TIMEOUT


@pytest.mark.asyncio
async def test_guppy_download_client_uses_configured_timeout():
    client = GuppyGQLClient(
        graphql_url="http://guppy-service/graphql",
        csrf_token_url="http://revproxy-service",
    )
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": {}}
    async_client = MagicMock()
    async_client.post = AsyncMock(return_value=response)
    async_client.__aenter__ = AsyncMock(return_value=async_client)
    async_client.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "gen3analysis.gen3.guppyQuery.httpx.AsyncClient", return_value=async_client
    ) as mock_async_client:
        await client.download(access_token="token", payload={})

    assert mock_async_client.call_args.kwargs["timeout"] == settings.GUPPY_HTTP_TIMEOUT


def test_es_client_uses_configured_timeout():
    from gen3analysis.gen3 import es_client

    es_client = importlib.reload(es_client)
    es_client.get_es.cache_clear()
    try:
        with patch("gen3analysis.gen3.es_client.Elasticsearch") as mock_elasticsearch:
            es_client.get_es()

        assert (
            mock_elasticsearch.call_args.kwargs["request_timeout"] == settings.ES_TIMEOUT
        )
        assert mock_elasticsearch.call_args.kwargs["timeout"] == settings.ES_TIMEOUT
    finally:
        es_client.get_es.cache_clear()
