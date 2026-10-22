from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from elasticsearch import Elasticsearch
from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette import status
from starlette.responses import JSONResponse

from gen3analysis.filters.gen3GQLFilters import (
    parse_gql_filter,
)
from gen3analysis.gen3.cursor import encode_cursor, decode_cursor
from gen3analysis.gen3.es_client import open_pit
from gen3analysis.query_builders.genomic.gene_facets import gene_facet_query
from gen3analysis.query_builders.genomic.queries import (
    query_top_genes,
    gene_table_query,
    query_top_ssm,
    gene_caseids_query,
)
from gen3analysis.query_builders.genomic.ssm import ssm_table_query
from gen3analysis.query_builders.genomic.ssm_facets import ssm_facet_query
from gen3analysis.settings import logger
from gen3analysis.settings import settings
from gen3analysis.utils.filters import project_filter

genomic = APIRouter()


def _build_agg_body(
    size: int,
    after_key: Optional[Dict[str, Any]],
    project: Optional[str],
    pit_id: str,
) -> Dict[str, Any]:
    """
    Build a search body with:
      - size: 0 (aggregation only)
      - composite agg 'by_gene' over TOP_GENES_GENE_ID_FIELD
      - nested sub-agg to cardinality count distinct cases
    """
    # Validate inputs
    if not pit_id or not isinstance(pit_id, str):
        raise ValueError("pit_id must be a non-empty string")

    if not isinstance(size, int) or size < 1 or size > 10000:
        raise ValueError("size must be an integer between 1 and 10000")

    # Validate required settings exist
    required_settings = [
        "TOP_GENES_GENE_ID_FIELD",
        "TOP_GENES_CASE_NESTED_PATH",
        "TOP_GENES_CASE_ID_FIELD",
        "ES_PIT_KEEP_ALIVE",
    ]
    for setting in required_settings:
        if not hasattr(settings, setting) or getattr(settings, setting) is None:
            raise ValueError(f"Required setting '{setting}' is not configured")

    filters = project_filter(project)

    # Ensure filters is a list
    if filters is None:
        filters = []
    elif not isinstance(filters, list):
        raise TypeError(f"project_filter must return a list, got {type(filters)}")

    # Build query - use bool filter even if empty for consistency
    query = {"bool": {"filter": filters}} if filters else {"match_all": {}}

    body: Dict[str, Any] = {
        "size": 0,
        "query": query,
        "aggs": {
            "by_gene": {
                "composite": {
                    "size": size,
                    "sources": [
                        {
                            "gene_id": {
                                "terms": {"field": settings.TOP_GENES_GENE_ID_FIELD}
                            }
                        }
                    ],
                    **({"after": after_key} if after_key else {}),
                },
                "aggs": {
                    "case_nested": {
                        "nested": {"path": settings.TOP_GENES_CASE_NESTED_PATH},
                        "aggs": {
                            "unique_cases": {
                                "cardinality": {
                                    "field": settings.TOP_GENES_CASE_ID_FIELD,
                                    "precision_threshold": 40000,
                                }
                            }
                        },
                    }
                },
            }
        },
        # Use PIT for consistency across pages. For searches with only aggs,
        # sort/_shard_doc isn't required, but harmless if included.
        "pit": {"id": pit_id, "keep_alive": settings.ES_PIT_KEEP_ALIVE},
        "track_total_hits": False,
    }
    return body


def fetch_top_genes_page(
    es: Elasticsearch,
    size: int,
    project: Optional[str],
    cursor: Optional[str],
    keep_alive_override: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    Returns (items, next_cursor)
    items: list of {gene_id, case_count, doc_count}
    next_cursor: opaque cursor with pit_id + after_key or None if last page
    """
    try:
        if cursor:
            decoded = decode_cursor(cursor)
            pit_id = decoded.get("pit_id")
            if not pit_id:
                raise ValueError("Decoded cursor missing 'pit_id'")
            after_key = decoded.get("after_key")
        else:
            pit_id = open_pit(
                settings.TOP_GENES_INDEX,
                keep_alive_override or settings.ES_PIT_KEEP_ALIVE,
            )
            after_key = None

        body = _build_agg_body(
            size=size, after_key=after_key, project=project, pit_id=pit_id
        )
        resp = es.search(body=body)

        # Validate response structure
        if "aggregations" not in resp or "by_gene" not in resp["aggregations"]:
            raise ValueError("Unexpected Elasticsearch response structure")

        buckets = resp["aggregations"]["by_gene"].get("buckets", [])
        next_after = resp["aggregations"]["by_gene"].get("after_key")

        items: List[Dict[str, Any]] = []
        for b in buckets:
            try:
                gene_id = b["key"]["gene_id"]
                doc_count = b["doc_count"]
                case_count = b["case_nested"]["unique_cases"]["value"]
                items.append(
                    {
                        "gene_id": gene_id,
                        "case_count": int(case_count),
                        "doc_count": int(doc_count),
                    }
                )
            except (KeyError, TypeError) as e:
                logger.warning(f"Skipping malformed bucket: {e}")
                continue

        if next_after:
            next_cursor = encode_cursor(resp.get("pit_id", pit_id), next_after)
        else:
            # No more pages; PIT will expire naturally
            next_cursor = None

        return items, next_cursor

    except Exception as e:
        logger.error(f"Error fetching top genes page: {e}", exc_info=True)
        raise


class TopGeneChartRequest(BaseModel):
    cohort_filter: Optional[Dict[str, Any]] = Field(
        default=None, description="Case filter (optional)"
    )
    gene_filter: Optional[Dict[str, Any]] = Field(
        default=None, description="Gene filter (optional)"
    )
    ssm_filter: Optional[Dict[str, Any]] = Field(
        default=None, description="Mutation filter (optional)"
    )
    size: Optional[int] = Field(default=20, ge=1, le=100000000)
    offset: Optional[int] = Field(default=0, ge=0)
    search: Optional[str] = Field(default=None, description="Search term (optional)")


@genomic.post(
    path="/gene_frequency_chart",
    status_code=status.HTTP_200_OK,
    description="Returns top genes filtered by cohort, gene, and ssm filters",
    summary="Top Genes by Cohort, Gene, and SSM",
    responses={
        status.HTTP_200_OK: {"description": "Successfully processed the chart query"},
        status.HTTP_400_BAD_REQUEST: {
            "description": "The request body is missing required fields or has invalid values."
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "User unauthorized when accessing endpoint"
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "User does not have access to requested data"
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Something went wrong internally when processing the request"
        },
    },
)
def gene_frequency_chart(body: TopGeneChartRequest):
    cohort_filter = parse_gql_filter(body.cohort_filter)
    gene_filter = parse_gql_filter(body.gene_filter)
    ssm_filter = parse_gql_filter(body.ssm_filter)

    chart_data = query_top_genes(
        case_filter=cohort_filter,
        gene_filter=gene_filter,
        ssm_filter=ssm_filter,
        size=body.size,
        offset=body.offset,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=chart_data,
    )


@genomic.post(
    path="/ssm_frequency_chart",
    status_code=status.HTTP_200_OK,
    description="Returns top ssms filtered by cohort, gene, and ssm filters",
    summary="Top SSMs by Cohort, Gene, and SSM",
    responses={
        status.HTTP_200_OK: {"description": "Successfully processed the chart query"},
        status.HTTP_400_BAD_REQUEST: {
            "description": "The request body is missing required fields or has invalid values."
        },
        status.HTTP_401_UNAUTHORIZED: {
            "description": "User unauthorized when accessing endpoint"
        },
        status.HTTP_403_FORBIDDEN: {
            "description": "User does not have access to requested data"
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Something went wrong internally when processing the request"
        },
    },
)
def ssm_frequency_chart(body: TopGeneChartRequest):
    cohort_filter = parse_gql_filter(body.cohort_filter)
    gene_filter = parse_gql_filter(body.gene_filter)
    ssm_filter = parse_gql_filter(body.ssm_filter)

    chart_data = query_top_ssm(
        case_filter=cohort_filter,
        gene_filter=gene_filter,
        ssm_filter=ssm_filter,
        size=body.size,
        offset=body.offset,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=chart_data,
    )


@genomic.post(
    path="/gene_table",
    status_code=status.HTTP_200_OK,
    description="Returns pages gene frequency table filtered by cohort, gene, and ssm filters",
    summary="Mutation Frequency: Gene Table",
    responses={
        status.HTTP_200_OK: {"description": "Successfully processed the table query"},
        status.HTTP_400_BAD_REQUEST: {
            "description": "The request body is missing required fields or has invalid values."
        },
    },
)
def gene_table(body: TopGeneChartRequest):
    cohort_filter = parse_gql_filter(body.cohort_filter)
    gene_filter = parse_gql_filter(body.gene_filter)
    ssm_filter = parse_gql_filter(body.ssm_filter)
    search = body.search
    size = body.size
    offset = body.offset

    table_data = gene_table_query(
        cohort_filter, gene_filter, ssm_filter, size, offset, search
    )

    return JSONResponse(status_code=status.HTTP_200_OK, content=table_data)


@genomic.post(path="/ssm_table")
def ssm_table(body: TopGeneChartRequest):
    cohort_filter = parse_gql_filter(body.cohort_filter)
    gene_filter = parse_gql_filter(body.gene_filter)
    ssm_filter = parse_gql_filter(body.ssm_filter)
    size = body.size
    offset = body.offset
    search = body.search

    table_data = ssm_table_query(cohort_filter, gene_filter, ssm_filter, size, offset)

    return JSONResponse(status_code=status.HTTP_200_OK, content=table_data)


class FacetRequest(BaseModel):
    cohort_filter: Optional[Dict[str, Any]] = Field(
        default=None, description="Case filter (optional)"
    )
    genomic_filter: Optional[Dict[str, Any]] = Field(
        default=None, description="Genomic filters"
    )


@genomic.post(path="/ssm_facets")
async def ssm_facets(
    body: FacetRequest,
):
    case_filter = parse_gql_filter(body.cohort_filter)
    genomic_filters = parse_gql_filter(body.genomic_filter)
    results = ssm_facet_query(case_filter, genomic_filters)

    return JSONResponse(status_code=status.HTTP_200_OK, content=results)


@genomic.post(path="/gene_facets")
async def gene_facets(body: FacetRequest):
    case_filter = parse_gql_filter(body.cohort_filter)
    genomic_filters = parse_gql_filter(body.genomic_filter)
    results = gene_facet_query(case_filter, genomic_filters)

    return JSONResponse(status_code=status.HTTP_200_OK, content=results)


class GeneCaseIdsRequest(BaseModel):
    cohort_filter: Optional[Dict[str, Any]] = Field(
        default=None, description="Case filter (optional)"
    )
    gene_filter: Optional[Dict[str, Any]] = Field(
        default=None, description="Gene filters"
    )
    ssm_filter: Optional[Dict[str, Any]] = Field(
        default=None, description="SSM filters"
    )


@genomic.post(path="/gene_cases")
async def gene_cases(body: GeneCaseIdsRequest):
    cohort_filter = parse_gql_filter(body.cohort_filter)
    gene_filter = parse_gql_filter(body.gene_filter)
    ssm_filter = parse_gql_filter(body.ssm_filter)
    results = gene_caseids_query(
        cohort_filter,
        gene_filter,
        ssm_filter,
    )

    return JSONResponse(status_code=status.HTTP_200_OK, content=results)
