from typing import Dict, Optional, List, Any
from glom import glom

from gen3analysis.filters.gen3GQLFilters import parse_gql_filter
from gen3analysis.query_builders.genomic.queries import query_case_ids, query_case_count
from gen3analysis.settings import logger

from gen3analysis.gen3.guppyQuery import GuppyGQLClient
from gen3analysis.settings import settings
from gen3analysis.utils.filterEdit import dot_notation_to_graphql


class CohortDownloadLimitError(ValueError):
    """The all-case export would exceed the bounded case-ID lookup."""


async def download_query(
    gen3_graphql_client: GuppyGQLClient,
    cohort_filter: Dict,
    filter: Dict,
    case_ids_filter_path: str,
    index: str,
    cohort_item_field: str = "case_id",
    case_index: str = "CaseCentric_case_centric",
    fields: Optional[List[str]] = None,
    access_token: Optional[str] = None,
):
    """Download cohort metadata; reject an oversized all-case ID lookup."""
    try:
        if not cohort_filter and query_case_count([]) > settings.MAX_CASES:
            raise CohortDownloadLimitError(
                f"All-case export exceeds the supported {settings.MAX_CASES}-case "
                "limit. Select a smaller cohort."
            )

        cohort_filter_gql = parse_gql_filter(cohort_filter)
        case_ids = query_case_ids(cohort_filter_gql)

        # build a filter containing the cohort ids and merge with the other filters
        ids = case_ids

        if "and" in filter:
            filter["and"].append({"in": {case_ids_filter_path: ids}})
        else:
            filter["and"] = [{"in": {case_ids_filter_path: ids}}]

        payload = {
            "type": index,
            "filter": filter,
            "fields": fields,
        }

        results = await gen3_graphql_client.download(
            access_token=access_token,
            payload=payload,
        )

        return results
    except Exception as e:
        logger.error(f"Error while processing cohort query: {e}")
        raise e
