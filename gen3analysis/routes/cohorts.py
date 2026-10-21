from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi import Cookie
from pydantic import BaseModel, Field
from starlette import status
from starlette.responses import JSONResponse

from gen3analysis.auth import Auth
from gen3analysis.settings import logger
from gen3analysis.dependencies.guppy_client import get_guppy_client
from gen3analysis.gen3.guppyQuery import GuppyGQLClient
from gen3analysis.query_builders.cases import cases, download
from gen3analysis.settings import settings

cohorts = APIRouter()


class CohortQueryRequest(BaseModel):
    cohort_filter: Optional[Dict] = Field(
        default=None, description="case filter (optional)"
    )
    case_ids_filter_path: str = Field(description="path for the case ids in the query")
    filter: Optional[Dict] = Field(default=None, description="query filter (optional)")
    query: Optional[str] = Field(
        default="",
        description="the query to execute using the case ids found using the cohort_filter(optional)",
    )
    case_index: Optional[str] = Field(
        default=settings.case_centric_gql, description="case index to query"
    )
    cohort_item_field: Optional[str] = Field(
        default="case_id", description="identity field for the case"
    )
    limit: Optional[int] = Field(
        default=10000, description="set the number of responses (optional)"
    )
    sort: Optional[List[Dict]] = Field(
        default=None, description="set the sort order (optional)"
    )


@cohorts.post(
    path="/query",
    dependencies=[Depends(get_guppy_client)],
    status_code=status.HTTP_200_OK,
    description="Performs a cohort query and return the query for all items matching the ids.",
    summary="Queries for cohort ids and uses those ids as the cohort in the second query",
    responses={
        status.HTTP_200_OK: {"description": "Successfully processed the cohort query"},
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
async def cohort_query(
    body: CohortQueryRequest,
    access_token: Optional[str] = Cookie(default=None, alias="access_token"),
    gen3_graphql_client: GuppyGQLClient = Depends(get_guppy_client),
    auth: Auth = Depends(Auth),
) -> JSONResponse:
    cohort_filter = body.cohort_filter
    case_index = body.case_index
    cohort_item_field = body.cohort_item_field
    limit = body.limit
    query = body.query
    query_filter = body.filter
    sort = body.sort
    case_ids_filter_path = body.case_ids_filter_path

    if cohort_filter is None or len(cohort_filter) == 0:
        raise HTTPException(status_code=400, detail="Must have the cohort_query filter")

    if query_filter is None or len(query_filter) == 0:
        raise HTTPException(status_code=400, detail="Must have the query filter")

    if case_ids_filter_path is None or len(query_filter) == 0:
        raise HTTPException(status_code=400, detail="Must have the case_ids_filter")

    if query is None or len(query_filter) == 0:
        raise HTTPException(status_code=400, detail="Must have the query")

    try:
        data = await cases.cohort_query(
            gen3_graphql_client=gen3_graphql_client,
            case_index=case_index,
            cohort_item_field=cohort_item_field,
            query=query,
            cohort_filter=cohort_filter,
            filter=query_filter,
            case_ids_filter_path=case_ids_filter_path,
            limit=limit,
            sort=sort,
            access_token=access_token,
        )

        return JSONResponse(status_code=status.HTTP_200_OK, content=data)
    except Exception as e:
        logger.error(f"Error while processing cohort query: {e}")
        raise HTTPException(status_code=500, detail="Error with cohort query")


class CohortDownloadRequest(BaseModel):
    cohort_filter: Optional[Dict] = Field(
        default=None, description="case filter (optional)"
    )
    case_ids_filter_path: str = Field(description="path for the case ids in the query")
    filter: Optional[Dict] = Field(default=None, description="query filter (optional)")
    index: str = Field(description="index to download from")
    fields: Optional[List[str]] = Field(
        default=None, description="fields to download (optional)"
    )
    case_index: Optional[str] = Field(
        default=settings.case_centric_gql, description="case index to query"
    )
    cohort_item_field: Optional[str] = Field(
        default="case_id", description="identity field for the case"
    )


@cohorts.post(
    path="/download",
    dependencies=[Depends(get_guppy_client)],
    status_code=status.HTTP_200_OK,
    description="Performs a cohort query for the guppy download endpoint and return the query for all items matching the ids.",
    summary="Cohort centric download query",
    responses={
        status.HTTP_200_OK: {"description": "Successfully processed the cohort query"},
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
async def cohort_download_query(
    body: CohortDownloadRequest,
    access_token: Optional[str] = Cookie(default=None, alias="access_token"),
    gen3_graphql_client: GuppyGQLClient = Depends(get_guppy_client),
    auth: Auth = Depends(Auth),
) -> JSONResponse:
    """Export metadata with explicit filters, preserving caller authorization."""
    cohort_filter = body.cohort_filter
    case_index = body.case_index
    cohort_item_field = body.cohort_item_field
    query_filter = body.filter
    index = body.index
    fields = body.fields
    case_ids_filter_path = body.case_ids_filter_path

    # An explicit empty filter means all authorized cases/files. The frontend
    # sends {} when no demographic or repository facet is selected.
    if cohort_filter is None:
        raise HTTPException(status_code=400, detail="Must have cohort_filter")

    if query_filter is None:
        raise HTTPException(status_code=400, detail="Must have query_filter")

    if index is None or len(index) == 0:
        raise HTTPException(status_code=400, detail="Must have index")

    if not case_ids_filter_path:
        raise HTTPException(status_code=400, detail="Must have case_ids_filter_path")

    try:
        data = await download.download_query(
            gen3_graphql_client=gen3_graphql_client,
            index=index,
            fields=fields,
            cohort_filter=cohort_filter,
            filter=query_filter,
            case_ids_filter_path=case_ids_filter_path,
            access_token=access_token,
        )

        return JSONResponse(status_code=status.HTTP_200_OK, content=data)
    except download.CohortDownloadLimitError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as e:
        logger.error(f"Error while processing download query: {e}")
        raise HTTPException(status_code=500, detail="Error with download query")
