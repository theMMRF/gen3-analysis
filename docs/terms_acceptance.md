# Terms Acceptance Schema

The Terms & Conditions acceptance schema lives in the dedicated
`terms_acceptance` PostgreSQL database. It records available terms versions and
the user acceptances for those versions.

## Configuration

Configure the API through the same environment mechanism used by the rest of
this service.

For deployed environments, use the dedicated Terms DB settings. The database
password is not stored in these values. The app retrieves it from AWS Secrets
Manager using `TERMS_DB_SECRET_ARN`.

```bash
TERMS_DB_ENABLED=true
TERMS_DB_HOST=mmrf-terms-dev.example.us-east-1.rds.amazonaws.com
TERMS_DB_PORT=5432
TERMS_DB_NAME=terms_acceptance
TERMS_DB_USER=postgres
TERMS_DB_SECRET_ARN=arn:aws:secretsmanager:us-east-1:ACCOUNT_ID:secret:SECRET_NAME
TERMS_DB_SSL_MODE=verify-full
TERMS_DB_SSL_ROOT_CERT=/etc/ssl/certs/rds-global-bundle.pem
```

For local development, `TERMS_ACCEPTANCE_DATABASE_URL` can still be used as a
full connection URL override. If neither `TERMS_DB_ENABLED=true` nor
`TERMS_ACCEPTANCE_DATABASE_URL` is configured, the Terms API endpoints return
`503` because the database is unavailable.

## Local Testing With Docker Postgres

For local development, the fastest way to test the schema and API is to run a
temporary PostgreSQL container. The official `postgres` Docker image creates the
database, user, and password from environment variables on first startup.

```bash
docker run --name terms-acceptance-postgres \
  -e POSTGRES_DB=terms_acceptance \
  -e POSTGRES_USER=terms_user \
  -e POSTGRES_PASSWORD=terms_password \
  -p 5432:5432 \
  -d postgres:16
```

This creates a local database named `terms_acceptance` with user `terms_user`
and password `terms_password`, exposed on `localhost:5432`.

Add the local connection URL to the repo-level `.env` file:

```bash
TERMS_ACCEPTANCE_DATABASE_URL=postgresql+asyncpg://terms_user:terms_password@localhost:5432/terms_acceptance
```

Apply the schema from the repo root:

```bash
poetry run python bin/terms_acceptance.py apply-schema
```

For a quick smoke test, insert inline content as the current terms version:

```bash
poetry run python bin/terms_acceptance.py create-version \
  --version local-2026-05-21 \
  --content '<h1>Terms & Conditions</h1><p>Local test terms.</p>' \
  --content-format html \
  --make-current
```

For a more realistic local test, create a small local terms file, for example
`terms-local.html`:

```html
<h1>Terms & Conditions</h1>
<p>Local test terms.</p>
```

Insert the file content as the current terms version:

```bash
poetry run python bin/terms_acceptance.py create-version \
  --version local-2026-05-21 \
  --content-file ./terms-local.html \
  --content-format html \
  --make-current
```

Start the API locally:

```bash
poetry run uvicorn gen3analysis.main:app_instance --reload
```

Then verify the current terms endpoint:

```bash
curl http://localhost:8000/analysis/v0/terms/current
```

`GET /terms/status` and `POST /terms/acceptances` require a valid bearer token
because they need authenticated user identity claims:

```bash
curl http://localhost:8000/analysis/v0/terms/status \
  -H "Authorization: Bearer YOUR_TOKEN"

curl -X POST http://localhost:8000/analysis/v0/terms/acceptances \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"terms_version_id": 1}'
```

To reset the local database, remove the container and run the `docker run`
command again:

```bash
docker rm -f terms-acceptance-postgres
```

## Purpose

`terms_versions` stores each published Terms & Conditions version, including its
effective timestamp, optional source URL, rendered terms content, content format,
and whether it is the current version. A partial unique index enforces that at
most one version can be marked current.

`terms_acceptances` stores the acceptance event for a user and a specific terms
version. The table keeps user identity details available at acceptance time.

## Checking Latest Acceptance

Whether a user has accepted the latest terms is derived by checking for an
acceptance row linked to the terms version currently marked with
`is_current = true`.

```sql
select exists (
  select 1
  from terms_acceptances ta
  join terms_versions tv on tv.id = ta.terms_version_id
  where ta.user_id = :user_id
    and tv.is_current = true
) as has_accepted_latest_terms;
```

## API Endpoints

`GET /terms/current` returns the current terms version, including
`terms_content` and `content_format`, so the frontend can display the current
terms before acceptance.

`GET /terms/status` returns whether the authenticated user has accepted the
current terms version. The frontend can call this before allowing the user into
the site.

`POST /terms/acceptances` records acceptance for the current terms version. The
request body includes the `terms_version_id` that the frontend displayed:

```json
{
  "terms_version_id": 1
}
```

If the submitted version is no longer current, the API returns `409` so the
frontend can refresh and show the latest terms.

## Append-Only Acceptances

Acceptance records are append-only so the service preserves a historical audit
trail of which terms version each user accepted and when. When terms change, the
existing acceptance rows remain unchanged and users receive new acceptance rows
for the new version. The `(user_id, terms_version_id)` unique constraint prevents
duplicate acceptance rows for the same user and version.

## Creating A New Current Version

Create a new row in `terms_versions` for the new terms text, then mark it as the
current version in the same transaction that clears the previous current version.
The current short-term approach stores the rendered terms content directly in the
database, so the frontend can fetch and display the current version without
hard-coding the text into a frontend release. `terms_url` is optional and can be
left `null` when there is no canonical URL for the terms.

```sql
begin;

update terms_versions
set is_current = false
where is_current = true;

insert into terms_versions (
    version,
    effective_at,
    terms_url,
    terms_content,
    content_format,
    is_current
) values (
    '2026-05-21',
    now(),
    null,
    '<h1>Terms & Conditions</h1><p>...</p>',
    'html',
    true
);

commit;
```

The helper script can apply the schema and create versions from a rendered
content file:

```bash
poetry run python bin/terms_acceptance.py apply-schema

poetry run python bin/terms_acceptance.py create-version \
  --version 2026-05-21 \
  --content-file ./terms-2026-05-21.html \
  --content-format html \
  --make-current
```
