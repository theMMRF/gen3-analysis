# Metadata authentication

All HTTP data routes are guarded before request handlers run, including handlers that query Elasticsearch directly. GET/HEAD health and version routes are the only exceptions. Swagger and new routers are protected by default.

Supply `Authorization: Bearer <access-token>` or the browser's `access_token` cookie. A supplied Authorization header takes precedence; a malformed or rejected header does not fall back to a cookie. Arborist validates the token and authorizes `gen3-analysis/read` on `METADATA_AUTH_RESOURCE` (default `/mmrf_metadata`). This is a metadata-only permission, independent of repository file downloads.

Missing credentials return 401, rejected permissions return 403, and an unavailable authorization service returns 503. Arborist's 401/403 token rejections are preserved. There is no local-development authentication bypass and no fallback to the server's SDK identity. Keep `ARBORIST_URL` pointed at the trusted environment's Arborist service.

A context variable carries the approved token through the entire request, including sync handlers and downstream Guppy calls. Existing cookie-only handler signatures cannot discard a local frontend bearer token. The context is reset after the response and never stored on the shared Guppy client. Protected responses use `Cache-Control: private, no-store`.

The dev rollout requires the companion MMRF Guppy collection policy and ProteinPaint service/proxy changes. Deploying this image before those dependencies are ready will correctly deny unauthenticated server-to-server calls. See `mmrf_gen3/docs/dev-metadata-auth.md` for the coordinated rollout.

Run the tests with the repository's Python 3.9 environment:

```sh
ES_ENABLED=false poetry run pytest tests/security tests/test_terms.py tests/test_timeout_config.py tests/query_builders tests/test_survival.py tests/test_compare.py
```

Tests mock Arborist decisions and exercise the real cases-to-Guppy request path. A deployed test with valid, expired and invalid dev credentials and a browse-only account is still required.
