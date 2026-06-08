#!/usr/bin/env python
import argparse
import asyncio
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = (
    PROJECT_ROOT / "db" / "migrations" / "001_create_terms_acceptance_schema.sql"
)

load_dotenv(PROJECT_ROOT / ".env")

from gen3analysis.settings import settings  # noqa: E402
from gen3analysis.terms_acceptance.database import (  # noqa: E402
    create_terms_acceptance_engine,
    get_terms_database_config,
)


def get_engine(args: argparse.Namespace):
    if args.database_url:
        return create_async_engine(args.database_url)

    engine = create_terms_acceptance_engine(get_terms_database_config(settings))
    if engine is None:
        raise SystemExit(
            "Configure TERMS_DB_* or TERMS_ACCEPTANCE_DATABASE_URL, "
            "or pass --database-url to connect."
        )

    return engine


async def apply_schema(engine) -> None:
    try:
        migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
        async with engine.begin() as conn:
            for statement in migration_sql.split(";"):
                statement = statement.strip()
                if statement:
                    await conn.execute(text(statement))
    finally:
        await engine.dispose()


async def create_version(
    engine,
    version: str,
    content_path: Optional[Path],
    content: Optional[str],
    content_format: str,
    terms_url: Optional[str],
    effective_at: str,
    make_current: bool,
) -> None:
    terms_content = content
    if content_path:
        terms_content = content_path.read_text(encoding="utf-8")
    if terms_content is None:
        raise SystemExit("Pass either --content-file or --content.")

    try:
        async with engine.begin() as conn:
            if make_current:
                await conn.execute(
                    text(
                        "update terms_versions "
                        "set is_current = false "
                        "where is_current = true"
                    )
                )

            await conn.execute(
                text(
                    """
                    insert into terms_versions (
                        version,
                        effective_at,
                        terms_url,
                        terms_content,
                        content_format,
                        is_current
                    ) values (
                        :version,
                        case
                            when :effective_at = 'now' then now()
                            else cast(:effective_at as timestamptz)
                        end,
                        :terms_url,
                        :terms_content,
                        :content_format,
                        :is_current
                    )
                    """
                ),
                {
                    "version": version,
                    "effective_at": effective_at,
                    "terms_url": terms_url,
                    "terms_content": terms_content,
                    "content_format": content_format,
                    "is_current": make_current,
                },
            )
    finally:
        await engine.dispose()


ACCEPTANCE_EXPORT_QUERY = """
select
    ta.id,
    ta.user_id,
    ta.email,
    ta.name,
    ta.accepted_at,
    ta.created_at,
    tv.id as terms_version_id,
    tv.version as terms_version,
    tv.effective_at as terms_effective_at,
    tv.is_current as terms_is_current
from terms_acceptances ta
join terms_versions tv on tv.id = ta.terms_version_id
where 1 = 1
"""

EXPORT_CSV_FIELDNAMES = [
    "email",
    "name",
    "terms_version",
    "accepted_at",
    "terms_is_current",
    "user_id",
    "terms_effective_at",
    "terms_version_id",
    "created_at",
    "id",
]

EXPORT_CSV_HEADERS = {
    "email": "Email",
    "name": "Name",
    "terms_version": "Terms Version",
    "accepted_at": "Accepted At (UTC)",
    "terms_is_current": "Current Terms Version",
    "user_id": "User ID",
    "terms_effective_at": "Terms Effective At (UTC)",
    "terms_version_id": "Terms Version ID",
    "created_at": "Record Created At (UTC)",
    "id": "Acceptance Record ID",
}


def serialize_export_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def serialize_csv_value(key: str, value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M:%S UTC")
    if key == "terms_is_current":
        return "Yes" if value else "No"
    if value is None:
        return ""
    return value


def row_to_export_dict(row) -> dict[str, Any]:
    return {
        key: serialize_export_value(value)
        for key, value in row._mapping.items()
    }


async def export_acceptances(
    engine,
    output_path: Path,
    output_format: str,
    user_id: Optional[str],
    email: Optional[str],
    current_only: bool,
) -> int:
    query = ACCEPTANCE_EXPORT_QUERY
    params: dict[str, Any] = {}

    if user_id:
        query += " and ta.user_id = :user_id"
        params["user_id"] = user_id
    if email:
        query += " and lower(ta.email) = lower(:email)"
        params["email"] = email
    if current_only:
        query += " and tv.is_current = true"

    query += " order by ta.accepted_at desc, ta.id desc"

    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(query), params)
            rows = [row_to_export_dict(row) for row in result]

        output_path.parent.mkdir(parents=True, exist_ok=True)

        if output_format == "json":
            output_path.write_text(
                json.dumps(rows, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        else:
            with output_path.open("w", encoding="utf-8", newline="") as handle:
                handle.write("\ufeff")
                writer = csv.DictWriter(
                    handle,
                    fieldnames=EXPORT_CSV_FIELDNAMES,
                    extrasaction="ignore",
                )
                writer.writerow(
                    {
                        field: EXPORT_CSV_HEADERS[field]
                        for field in EXPORT_CSV_FIELDNAMES
                    }
                )
                for row in rows:
                    writer.writerow(
                        {
                            field: serialize_csv_value(field, row.get(field))
                            for field in EXPORT_CSV_FIELDNAMES
                        }
                    )

        return len(rows)
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the terms_acceptance database schema and versions."
    )
    parser.add_argument("--database-url", help="Overrides TERMS_ACCEPTANCE_DATABASE_URL")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("apply-schema", help="Apply the terms acceptance schema")

    create_parser = subparsers.add_parser(
        "create-version", help="Create a terms version from a content file"
    )
    create_parser.add_argument("--version", required=True)
    content_group = create_parser.add_mutually_exclusive_group(required=True)
    content_group.add_argument("--content-file", type=Path)
    content_group.add_argument("--content")
    create_parser.add_argument(
        "--content-format",
        choices=["html", "markdown", "plain_text"],
        default="html",
    )
    create_parser.add_argument("--terms-url")
    create_parser.add_argument(
        "--effective-at",
        default="now",
        help="Effective timestamp, or 'now' to use the database current time",
    )
    create_parser.add_argument("--make-current", action="store_true")

    export_parser = subparsers.add_parser(
        "export-acceptances",
        help="Export terms acceptance records to CSV or JSON",
    )
    export_parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path to write the export file",
    )
    export_parser.add_argument(
        "--format",
        choices=["csv", "json"],
        default="csv",
        help="Export format (default: csv)",
    )
    export_parser.add_argument(
        "--user-id",
        help="Filter to a single user_id (JWT sub claim)",
    )
    export_parser.add_argument(
        "--email",
        help="Filter to a single email address",
    )
    export_parser.add_argument(
        "--current-only",
        action="store_true",
        help="Only include acceptances for the current terms version",
    )

    return parser


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    engine = get_engine(args)

    if args.command == "apply-schema":
        await apply_schema(engine)
    elif args.command == "create-version":
        await create_version(
            engine=engine,
            version=args.version,
            content_path=args.content_file,
            content=args.content,
            content_format=args.content_format,
            terms_url=args.terms_url,
            effective_at=args.effective_at,
            make_current=args.make_current,
        )
    elif args.command == "export-acceptances":
        row_count = await export_acceptances(
            engine=engine,
            output_path=args.output,
            output_format=args.format,
            user_id=args.user_id,
            email=args.email,
            current_only=args.current_only,
        )
        print(f"Exported {row_count} acceptance record(s) to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
