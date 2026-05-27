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

The Docker image downloads the RDS global CA bundle to that path at build time.
No separate Kubernetes mount is required for deployed environments.

For local development, `TERMS_ACCEPTANCE_DATABASE_URL` can still be used as a
full connection URL override. If neither `TERMS_DB_ENABLED=true` nor
`TERMS_ACCEPTANCE_DATABASE_URL` is configured, the Terms API endpoints return
`503` because the database is unavailable.

Configuration is read from **Kubernetes deployment environment variables** (or a
repo-level `.env` file for local dev). The legacy `config.py` Kubernetes secret
is not used by this service.

## Deployed Environment Setup

Use this checklist when standing up Terms acceptance in dev or prod.

### 1. RDS and Secrets Manager

- Create a dedicated PostgreSQL database named `terms_acceptance`.
- Store the master password in AWS Secrets Manager (RDS-managed secrets are fine).
- Note the RDS hostname and the Secrets Manager ARN (`TERMS_DB_SECRET_ARN`).

### 2. GitOps environment variables

Add these to the gen3-analysis deployment in gitops (dev/prod values as
appropriate):

```yaml
- name: TERMS_DB_ENABLED
  value: "true"
- name: TERMS_DB_HOST
  value: "mmrf-terms-dev.example.us-east-1.rds.amazonaws.com"
- name: TERMS_DB_PORT
  value: "5432"
- name: TERMS_DB_NAME
  value: "terms_acceptance"
- name: TERMS_DB_USER
  value: "postgres"
- name: TERMS_DB_SECRET_ARN
  value: "arn:aws:secretsmanager:us-east-1:ACCOUNT_ID:secret:rds!db-..."
- name: TERMS_DB_SSL_MODE
  value: "verify-full"
- name: TERMS_DB_SSL_ROOT_CERT
  value: "/etc/ssl/certs/rds-global-bundle.pem"
```

Deploy a gen3-analysis image that includes the RDS CA bundle (built from this
repo's Dockerfile). No separate Kubernetes mount is required for the cert.

After rollout, confirm the pod has the variables:

```bash
kubectl exec deploy/gen3-analysis-deployment -- env | grep TERMS_
```

### 3. RDS security groups

Allow PostgreSQL (`5432/tcp`) from the EKS worker node security group to the
RDS instance security group. Without this, the API and helper script can read
the password from Secrets Manager but cannot connect to the database.

If you run database admin commands from a squid/bastion host using direct RDS
access, also allow that host's security group (or your admin VPN CIDR) on the
RDS security group.

### 4. IAM permissions for Secrets Manager

The gen3-analysis pod retrieves the database password at startup using boto3 and
`TERMS_DB_SECRET_ARN`. Attach a policy like this to the IAM role used by the
pod.

When IRSA is not configured, pods use the **EKS worker node role** (for example
`eks_mmrf-dev-dl_workers_role`). When IRSA is configured, attach the policy to
the gen3-analysis service account role instead.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "TermsDbSecretRead",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret"
      ],
      "Resource": "arn:aws:secretsmanager:us-east-1:ACCOUNT_ID:secret:rds!db-045bdc7b-13c2-4022-aa57-420f1d173d84-*"
    }
  ]
}
```

Use the `-*` suffix wildcard because Secrets Manager ARNs include a random
suffix. Replace `ACCOUNT_ID` and the secret name with your environment values.

If the secret uses a customer-managed KMS key, you may also need `kms:Decrypt`
on that key. RDS-managed secrets usually do not require an extra KMS policy.

After updating IAM, restart the deployment if the pod was crash-looping:

```bash
kubectl rollout restart deployment/gen3-analysis-deployment
kubectl logs -f deploy/gen3-analysis-deployment
```

### 5. Apply schema and load the current terms version

Run these from a squid/admin host with `kubectl` access to the cluster.

**Apply the schema** (one time per environment):

```bash
kubectl exec deploy/gen3-analysis-deployment -- \
  bash -c 'cd /gen3analysis && poetry run python bin/terms_acceptance.py apply-schema'
```

The helper runs inside the pod and uses the pod's `TERMS_DB_*` environment
variables and IAM role. You do not need Secrets Manager permissions on the
squid instance itself for this workflow.

**Create a terms HTML file on squid** using a quoted heredoc so pasted HTML is
not interpreted by the shell (safe for `$`, backticks, and quotes):

```bash
cat > /tmp/terms-dev.html <<'EOF'
(paste your full HTML here)
EOF
```

Copy the file into the pod:

```bash
kubectl cp /tmp/terms-dev.html \
  gen3-analysis-deployment:/gen3analysis/terms-dev.html
```

If multiple gen3-analysis pods exist during a rollout, copy to a specific pod
name or wait until only one ready pod remains:

```bash
kubectl get pods | grep gen3-analysis
kubectl cp /tmp/terms-dev.html POD_NAME:/gen3analysis/terms-dev.html
```

**Load the terms version and mark it current:**

```bash
kubectl exec deploy/gen3-analysis-deployment -- \
  bash -c 'cd /gen3analysis && poetry run python bin/terms_acceptance.py create-version \
    --version dev-2026-05-22 \
    --content-file ./terms-dev.html \
    --content-format html \
    --make-current'
```

Use a meaningful `--version` string for each release (for example
`2026-05-22` in prod).

**Verify:**

```bash
kubectl exec deploy/gen3-analysis-deployment -- \
  curl -s http://127.0.0.1:8000/analysis/v0/terms/current
```

From outside the cluster (through revproxy):

```bash
curl -s https://YOUR_HOST/analysis/v0/terms/current
```

Before a terms row exists, `/terms/current` returns an error indicating no
current version is configured. After `create-version --make-current`, it
returns JSON with `terms_content`.

### 6. Publishing updated terms later

Create a new version with a new `--version` value and `--make-current`. The
helper clears the previous current flag in the same transaction.

```bash
kubectl cp /tmp/terms-updated.html POD_NAME:/gen3analysis/terms-updated.html

kubectl exec deploy/gen3-analysis-deployment -- \
  bash -c 'cd /gen3analysis && poetry run python bin/terms_acceptance.py create-version \
    --version 2026-06-01 \
    --content-file ./terms-updated.html \
    --content-format html \
    --make-current'
```

Users who accepted an older version will need to accept again; existing
acceptance rows are preserved for audit history.

### 7. Verify acceptance and export records

#### Check via the API

Use your bearer token to confirm the authenticated user accepted the current
terms version:

```bash
curl -s https://YOUR_HOST/analysis/v0/terms/status \
  -H "Authorization: Bearer YOUR_TOKEN"
```

Expected response when accepted:

```json
{
  "has_accepted_latest_terms": true,
  "current_terms": {
    "id": 1,
    "version": "dev-2026-05-22",
    "...": "..."
  }
}
```

From inside the pod:

```bash
kubectl exec deploy/gen3-analysis-deployment -- \
  curl -s http://127.0.0.1:8000/analysis/v0/terms/status \
    -H "Authorization: Bearer YOUR_TOKEN"
```

#### Export acceptance records from the database

The helper script exports acceptance rows joined with terms version metadata to
CSV by default. CSV opens directly in Excel and is suitable to share with legal
or compliance reviewers. Run the export inside the pod, copy the file to squid,
then upload to S3.

Export all acceptance records:

```bash
kubectl exec deploy/gen3-analysis-deployment -- \
  bash -c 'cd /gen3analysis && poetry run python bin/terms_acceptance.py export-acceptances \
    --output /tmp/terms-acceptances.csv'
```

Export only the current terms version:

```bash
kubectl exec deploy/gen3-analysis-deployment -- \
  bash -c 'cd /gen3analysis && poetry run python bin/terms_acceptance.py export-acceptances \
    --output /tmp/terms-acceptances-current.csv \
    --current-only'
```

Export a single user by email or JWT `sub` (`user_id`):

```bash
kubectl exec deploy/gen3-analysis-deployment -- \
  bash -c 'cd /gen3analysis && poetry run python bin/terms_acceptance.py export-acceptances \
    --output /tmp/terms-acceptance-user.csv \
    --email you@example.com'

kubectl exec deploy/gen3-analysis-deployment -- \
  bash -c 'cd /gen3analysis && poetry run python bin/terms_acceptance.py export-acceptances \
    --output /tmp/terms-acceptance-user.csv \
    --user-id YOUR_JWT_SUB'
```

JSON export is also supported with `--format json`.

Copy the export file from the pod to squid:

```bash
kubectl cp deploy/gen3-analysis-deployment:/tmp/terms-acceptances.csv \
  /tmp/terms-acceptances.csv
```

If `kubectl cp` fails during a rollout, copy from a specific pod name instead.

Upload to S3:

```bash
aws s3 cp /tmp/terms-acceptances.csv \
  s3://YOUR_BUCKET/path/terms-acceptances/terms-acceptances-$(date -u +%Y%m%dT%H%M%SZ).csv
```

CSV columns include human-readable headers such as:

- `Email`, `Name`
- `Terms Version`, `Accepted At (UTC)`, `Current Terms Version` (`Yes`/`No`)
- `User ID`, `Terms Effective At (UTC)`, `Terms Version ID`

The export does not include full `terms_content` text, only version metadata and
acceptance audit fields.

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
