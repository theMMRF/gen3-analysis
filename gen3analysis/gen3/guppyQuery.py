import asyncio
import json
from typing import Dict, Any, Optional

from fastapi import Cookie, HTTPException
import httpx
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR

from gen3analysis.settings import logger, settings
from gen3analysis.gen3.csrfTokenCache import CSRFTokenCache


class GuppyGQLClient:
    def __init__(self, graphql_url: str, csrf_token_url: str):
        self.graphql_url = graphql_url
        self.download_url = graphql_url.replace("/graphql", "/download")
        self.csrf_cache = CSRFTokenCache(
            rest_api_url=f"{csrf_token_url}/_status",
            token_ttl_seconds=3600,  # 1 hour
        )
        self._http_client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared async HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=settings.GUPPY_HTTP_TIMEOUT,
                limits=httpx.Limits(
                    max_keepalive_connections=20,
                    max_connections=100,
                    keepalive_expiry=30.0,
                ),
            )
        return self._http_client

    async def close(self):
        """Close the HTTP client connection pool."""
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def execute(
        self,
        access_token: str,
        query: str,
        variables: Dict[str, Any] = None,
        retry_count: int = 3,
    ) -> Dict[str, Any]:
        for attempt in range(retry_count + 1):
            try:
                #    csrf_token = await self.csrf_cache.get_token()
                headers = {
                    "Content-Type": "application/json",
                    #        "X-CSRF-Token": csrf_token,
                }
                if access_token:
                    headers["Authorization"] = f"Bearer {access_token}"

                payload = {"query": query, "variables": variables or {}}

                client = self._get_client()
                response = await client.post(
                    self.graphql_url, json=payload, headers=headers
                )

                if response.status_code != 200:
                    if attempt < retry_count:
                        continue
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"GraphQL request failed: {response.text}",
                    )

                result = response.json()

                # Check for CSRF-related errors
                if self._is_csrf_error(result):
                    if attempt < retry_count:
                        await self.csrf_cache._refresh_token()  # Force refresh
                        continue

                if result.get("errors"):
                    err_msg = (
                        f"GuppyGQLClient error: {result['errors']} for query: {query}"
                    )
                    logger.error(err_msg)
                    raise HTTPException(
                        status_code=HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=err_msg,
                    )

                return result

            except Exception as e:
                # log exception
                logger.error(f"GuppyGQLClient error: {str(e)}")
                if attempt == retry_count:
                    raise
                await asyncio.sleep(0.1 * (2**attempt))  # Exponential backoff

    async def download(
        self,
        access_token: str,
        payload: Dict,
        retry_count: int = 3,
    ) -> Dict[str, Any]:
        for attempt in range(retry_count + 1):
            try:
                #    csrf_token = await self.csrf_cache.get_token()
                headers = {
                    "Content-Type": "application/json",
                    #        "X-CSRF-Token": csrf_token,
                }
                if access_token:
                    headers["Authorization"] = f"Bearer {access_token}"

                async with httpx.AsyncClient(
                    timeout=settings.GUPPY_HTTP_TIMEOUT
                ) as client:
                    response = await client.post(
                        self.download_url, json=payload, headers=headers
                    )

                if response.status_code != 200:
                    if attempt < retry_count:
                        continue
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"GraphQL request failed: {response.text}",
                    )

                result = response.json()
                return result

            except Exception as e:
                # log exception
                logger.error(f"GuppyGQLClient download error: {str(e)}")
                if attempt == retry_count:
                    raise
                await asyncio.sleep(0.1 * (2**attempt))  # Exponential backoff

    def _is_csrf_error(self, result: Dict[str, Any]) -> bool:
        errors = result.get("errors", [])
        return any("csrf" in str(error).lower() for error in errors)
