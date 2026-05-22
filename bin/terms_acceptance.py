#!/usr/bin/env python
import argparse
import asyncio
from pathlib import Path
from typing import Optional

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


if __name__ == "__main__":
    asyncio.run(main())
