-- Schema for the dedicated terms_acceptance PostgreSQL database.

CREATE TABLE terms_versions (
    id bigserial PRIMARY KEY,
    version text NOT NULL UNIQUE,
    effective_at timestamptz NOT NULL,
    terms_url text,
    terms_content text NOT NULL,
    content_format text NOT NULL DEFAULT 'html',
    is_current boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT terms_versions_content_format_check
        CHECK (content_format IN ('html', 'markdown', 'plain_text'))
);

CREATE UNIQUE INDEX terms_versions_one_current_idx
    ON terms_versions (is_current)
    WHERE is_current = true;

CREATE TABLE terms_acceptances (
    id bigserial PRIMARY KEY,
    user_id text NOT NULL,
    email text NOT NULL,
    name text,
    terms_version_id bigint NOT NULL REFERENCES terms_versions (id),
    accepted_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT terms_acceptances_user_version_unique
        UNIQUE (user_id, terms_version_id)
);

CREATE INDEX terms_acceptances_user_id_idx
    ON terms_acceptances (user_id);

CREATE INDEX terms_acceptances_lower_email_idx
    ON terms_acceptances (lower(email));

CREATE INDEX terms_acceptances_terms_version_id_idx
    ON terms_acceptances (terms_version_id);
