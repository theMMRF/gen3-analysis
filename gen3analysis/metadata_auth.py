"""Authenticate every data request, including queries which bypass Guppy."""

from contextvars import ContextVar
from typing import Optional

from gen3authz.client.arborist.errors import ArboristError
from starlette.requests import Request
from starlette.responses import JSONResponse

from gen3analysis.settings import settings

request_access_token: ContextVar[Optional[str]] = ContextVar(
    "request_access_token", default=None
)


def caller_token(request: Request) -> Optional[str]:
    # A supplied but invalid header must never fall back to a valid cookie.
    header = request.headers.get("authorization")
    if header is not None:
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return None
        return parts[1]
    return request.cookies.get("access_token")


class MetadataAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        prefix = settings.URL_PREFIX.rstrip("/")
        if prefix and path.startswith(prefix + "/"):
            path = path[len(prefix) :]
        # Only non-data health/version routes are public. OPTIONS, docs and new
        # routers are protected too; no frontend or local-development bypass.
        if scope["method"] in ("GET", "HEAD") and path.rstrip("/") in (
            "/_status",
            "/_version",
        ):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        token = caller_token(request)
        if not token:
            await JSONResponse(
                {"detail": "An access token is required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
            )(scope, receive, send)
            return

        try:
            # Arborist validates the supplied token and checks the metadata-only
            # policy. Do not infer authorization by decoding an unverified JWT.
            authorized = await scope["app"].state.arborist_client.auth_request(
                token, "gen3-analysis", "read", [settings.METADATA_AUTH_RESOURCE]
            )
        except ArboristError as exc:
            status = exc.code if exc.code in (401, 403) else 503
            await JSONResponse(
                {
                    "detail": (
                        "Metadata access denied"
                        if status != 503
                        else "Authorization service unavailable"
                    )
                },
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        except Exception:
            await JSONResponse(
                {"detail": "Authorization service unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        if authorized is not True:
            await JSONResponse(
                {"detail": "Metadata access denied"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return

        context = request_access_token.set(token)
        scope.setdefault("state", {})["metadata_access_token"] = token

        async def private_response(message):
            if message["type"] == "http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"private, no-store"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, private_response)
        finally:
            request_access_token.reset(context)
