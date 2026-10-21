"""Explicit empty export filters are valid; missing filters remain invalid."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from gen3analysis.routes.cohorts import CohortDownloadRequest, cohort_download_query
from gen3analysis.query_builders.cases import download
from gen3analysis.settings import settings


class CohortDownloadValidation(unittest.IsolatedAsyncioTestCase):
    def request(self, **changes):
        return CohortDownloadRequest(
            **{
                "cohort_filter": {},
                "filter": {},
                "index": "file",
                "case_ids_filter_path": "cases.case_id",
                **changes,
            }
        )

    async def test_empty_filters_and_populated_filters_preserve_authorization_token(
        self,
    ):
        for cohort, files in [
            ({}, {}),
            ({"in": {"case_id": ["test-case"]}}, {}),
            ({}, {"in": {"data_format": ["BAM"]}}),
        ]:
            with self.subTest(cohort=cohort, files=files):
                with patch(
                    "gen3analysis.routes.cohorts.download.download_query",
                    new_callable=AsyncMock,
                    return_value=[],
                ) as query:
                    response = await cohort_download_query(
                        self.request(cohort_filter=cohort, filter=files),
                        access_token="test-only-token",
                        gen3_graphql_client=object(),
                        auth=object(),
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(json.loads(response.body), [])
                    self.assertEqual(query.await_args.kwargs["cohort_filter"], cohort)
                    self.assertEqual(query.await_args.kwargs["filter"], files)
                    self.assertEqual(
                        query.await_args.kwargs["access_token"], "test-only-token"
                    )

    async def test_missing_filters_and_empty_required_paths_are_rejected(self):
        for changes in [
            {"cohort_filter": None},
            {"filter": None},
            {"index": ""},
            {"case_ids_filter_path": ""},
        ]:
            with self.subTest(changes=changes):
                with patch(
                    "gen3analysis.routes.cohorts.download.download_query",
                    new_callable=AsyncMock,
                ) as query:
                    with self.assertRaises(HTTPException) as failure:
                        await cohort_download_query(
                            self.request(**changes), auth=object()
                        )
                    self.assertEqual(failure.exception.status_code, 400)
                    query.assert_not_awaited()

    async def test_oversized_all_case_export_fails_before_collecting_or_downloading(
        self,
    ):
        with patch.object(
            download, "query_case_count", return_value=settings.MAX_CASES + 1
        ), patch.object(download, "query_case_ids") as ids:
            client = AsyncMock()
            with self.assertRaises(download.CohortDownloadLimitError):
                await download.download_query(client, {}, {}, "cases.case_id", "file")
            ids.assert_not_called()
            client.download.assert_not_awaited()

    async def test_limit_is_inclusive_and_filtered_cohort_behavior_is_unchanged(self):
        for cohort in [{}, {"in": {"case_id": ["test-case"]}}]:
            with self.subTest(cohort=cohort), patch.object(
                download, "query_case_count", return_value=settings.MAX_CASES
            ) as count, patch.object(
                download, "query_case_ids", return_value=["test-case"]
            ):
                client = AsyncMock()
                client.download.return_value = []
                await download.download_query(
                    client,
                    cohort,
                    {},
                    "cases.case_id",
                    "file",
                    access_token="test-only-token",
                )
                self.assertEqual(
                    client.download.await_args.kwargs["access_token"], "test-only-token"
                )
                self.assertEqual(
                    client.download.await_args.kwargs["payload"]["filter"],
                    {"and": [{"in": {"cases.case_id": ["test-case"]}}]},
                )
                self.assertEqual(count.call_count, 0 if cohort else 1)

    async def test_export_limit_is_reported_as_client_error(self):
        with patch.object(
            download,
            "download_query",
            new_callable=AsyncMock,
            side_effect=download.CohortDownloadLimitError("Select a smaller cohort"),
        ):
            with self.assertRaises(HTTPException) as failure:
                await cohort_download_query(self.request(), auth=object())
            self.assertEqual(failure.exception.status_code, 400)
