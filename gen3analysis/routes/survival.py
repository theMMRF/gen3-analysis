import json
from typing import List, Dict, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi import Cookie
from glom import glom
from lifelines import KaplanMeierFitter
from lifelines.statistics import multivariate_logrank_test
from pydantic import BaseModel, Field
from starlette import status
from starlette.responses import JSONResponse

from gen3analysis.auth import Auth
from gen3analysis.settings import logger
from gen3analysis.dependencies.guppy_client import get_guppy_client
from gen3analysis.filters.gen3GQLFilters import parse_gql_filter
from gen3analysis.gen3.guppyQuery import GuppyGQLClient
from gen3analysis.query_builders.cases import cases
from gen3analysis.query_builders.genomic.survival import (
    genomic_survival_comparison_query,
)
from gen3analysis.settings import settings

survival = APIRouter()

Gen3GraphQLQuery = f"""query SurvivalCaseQuery($filter: JSON) {{
    {settings.case_centric_gql}(accessibility: accessible, offset: 0, first: {settings.MAX_CASES}, filter: $filter) {{
        submitter_id
        case_id
        project {{
            project_id
        }}
        demographic {{
            days_to_death
            vital_status
        }}
        diagnoses {{
            days_to_last_follow_up
        }}
        outcomes {{
            survival_time_pfs
            censor_pfs
        }}
    }}
    {settings.case_centric_agg_gql} {{
        {settings.CASE_CENTRIC_INDEX}(filter: $filter, accessibility: accessible) {{
            _totalCount
        }}
    }}
}}
"""


def parse_event_flag(value) -> Optional[int]:
    """Parse a submitted 0/1 or boolean-like event flag."""
    if value is None:
        return None

    if isinstance(value, bool):
        return int(value)

    if isinstance(value, (int, float)):
        if value in {0, 1}:
            return int(value)
        return None

    if isinstance(value, str):
        normalized_value = value.strip().lower()
        if normalized_value in {"1", "true", "yes", "y"}:
            return 1
        if normalized_value in {"0", "false", "no", "n"}:
            return 0
        try:
            numeric_value = float(normalized_value)
        except ValueError:
            return None
        return parse_event_flag(numeric_value)

    return None


def transform_overall_survival(data) -> pd.DataFrame:
    """Transform the Gen3 data into a pandas DataFrame suitable for lifelines."""
    records = []

    for case in data:
        demographic = case.get("demographic", [])
        if not demographic:
            continue

        demo = demographic
        days_to_death = demo.get("days_to_death")
        diag = case.get("diagnoses")
        if diag is None:
            days_to_follow_up = None
        else:
            diagnoses = case.get("diagnoses", [None])[0]
            days_to_follow_up = diagnoses.get("days_to_last_follow_up")

        # Use days_to_death if available, otherwise use days_to_follow_up
        duration = days_to_death if days_to_death is not None else days_to_follow_up

        if duration is not None:
            records.append(
                {
                    "duration": duration,
                    "event": int(
                        demo.get("vital_status", "").lower() != "alive"
                    ),  # 1 if dead, 0 if alive
                    "case_id": case.get("case_id"),
                    "submitter_id": case.get("submitter_id"),
                    "project_id": glom(case, "project.project_id", default="---"),
                }
            )

    return pd.DataFrame(records)


def transform_progression_free_survival(data) -> pd.DataFrame:
    """Transform PFS outcomes into a pandas DataFrame suitable for lifelines."""
    records = []

    for case in data:
        outcomes = case.get("outcomes") or []
        if isinstance(outcomes, dict):
            outcomes = [outcomes]

        for outcome in outcomes:
            duration = outcome.get("survival_time_pfs")
            event = parse_event_flag(outcome.get("censor_pfs"))
            if duration is None or event is None:
                continue

            records.append(
                {
                    "duration": duration,
                    "event": event,
                    "case_id": case.get("case_id"),
                    "submitter_id": case.get("submitter_id"),
                    "project_id": glom(case, "project.project_id", default="---"),
                }
            )
            break

    return pd.DataFrame(records)


async def get_curve(filters, gen3_graphql_client, access_token=None):
    query_filter = {
        "and": [
            filters,
            {
                "or": [
                    {
                        ">": {"demographic.days_to_death": 0},
                    },
                    {
                        ">": {"diagnoses.days_to_last_follow_up": 0},
                    },
                    {
                        ">": {"outcomes.survival_time_pfs": 0},
                    },
                ]
            },
        ]
    }
    data = await gen3_graphql_client.execute(
        access_token=access_token,
        query=Gen3GraphQLQuery,
        variables={"filter": query_filter},
        retry_count=1,
    )

    if (
        glom(
            data,
            f"data.{settings.case_centric_agg_gql}.{settings.CASE_CENTRIC_INDEX}._totalCount",
            default=0,
        )
        == 0
    ):
        return None
    data_root = glom(data, f"data.{settings.case_centric_gql}", default={})
    return {
        "overall_survival": calculate_curve(transform_overall_survival(data_root)),
        "progression_free_survival": calculate_curve(
            transform_progression_free_survival(data_root)
        ),
    }


def calculate_curve(df: pd.DataFrame):
    if df.empty:
        return None

    # Ensure duration and event columns are numeric
    df["duration"] = pd.to_numeric(df["duration"], errors="coerce")
    df["event"] = pd.to_numeric(df["event"], errors="coerce").astype(int)

    # Remove rows with NaN values
    df = df.dropna(subset=["duration", "event"])

    if df.empty:
        return None

    # Create KaplanMeierFitter object
    kmf = KaplanMeierFitter()

    # return these for use in the statistics calculation
    durations = df["duration"]
    events = df["event"]
    kmf.fit(durations=durations, event_observed=events, label="Survival Curve")

    # Group donors by their exact duration time
    donors_by_time = (
        df.groupby("duration")
        .apply(
            lambda group: [
                {
                    "id": row["case_id"],
                    "submitter_id": row["submitter_id"],
                    "project_id": row["project_id"],
                    "event_observed": row["event"] == 1,
                }
                for _, row in group.iterrows()
            ]
        )
        .to_dict()
    )

    # Format results
    results = []
    survival_df = kmf.survival_function_

    # Create a sorted list of time points
    timeline_sorted = sorted(kmf.timeline)

    for i, time_point in enumerate(timeline_sorted):
        survival_prob = survival_df.loc[time_point].iloc[0]

        # Only include donors who have events at this exact time point
        if time_point in donors_by_time:
            for donor in donors_by_time[time_point]:
                # For donors with events at this time point, use the survival probability
                # from BEFORE the event (i.e., the previous time point or current if censored)

                if donor["event_observed"]:
                    # For observed events, use survival probability from the previous time point
                    if i > 0:
                        prev_time_point = timeline_sorted[i - 1]
                        donor_survival_prob = survival_df.loc[prev_time_point].iloc[0]
                    else:
                        # If this is the first time point, use 1.0
                        donor_survival_prob = 1.0
                else:
                    # For censored events, use the current survival probability
                    donor_survival_prob = survival_prob

                results.append(
                    {
                        "time": int(time_point),
                        "id": donor["id"],
                        "submitter_id": donor["submitter_id"],
                        "project_id": donor["project_id"],
                        "survivalEstimate": float(donor_survival_prob),
                        "censored": not donor["event_observed"],
                    }
                )

    return {
        "meta": {"id": id(results)},
        "donors": results,
        "durations": durations,
        "events": events,
    }


def calculate_survival_statistics(non_empty_curves: List[Dict]) -> Dict:
    """
    Calculate survival statistics for multiple curves using a log-rank test.

    Args:
        non_empty_curves: List of curve dictionaries containing durations, events, and donors

    Returns:
        Dictionary containing pValue and degreesFreedom, or empty dict if < 2 curves
    """
    statistics = {}
    if len(non_empty_curves) > 1:
        all_durations = []
        all_events = []
        for curve in non_empty_curves:
            all_durations.extend(curve["durations"])
            all_events.extend(curve["events"])

        groups = []
        for curve_index in range(len(non_empty_curves)):
            groups.extend([curve_index] * len(non_empty_curves[curve_index]["donors"]))

        log_rank_results = multivariate_logrank_test(all_durations, groups, all_events)
        statistics = {
            "pValue": log_rank_results.p_value,
            "degreesFreedom": len(non_empty_curves) - 1,
        }

    return statistics


# Define a Pydantic model for the request body
class PlotRequest(BaseModel):
    filters: List[Dict]


@survival.post(
    path="/",
    dependencies=[Depends(get_guppy_client)],
    status_code=status.HTTP_200_OK,
    description="Retrieves the survival plot(s) for the given filters. An array of filters is provided and will return an array of survival plot data",
    summary="Survival plots for cohort represented as filters",
    responses={
        status.HTTP_200_OK: {"description": "Successfully processed the survival plot"},
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
async def plot(
    body: PlotRequest,
    access_token: Optional[str] = Cookie(None),
    gen3_graphql_client: GuppyGQLClient = Depends(get_guppy_client),
    auth: Auth = Depends(Auth),
) -> JSONResponse:
    filters = body.filters

    if filters is None or len(filters) == 0:
        raise HTTPException(status_code=400, detail="Must have at least one filter")

    try:
        overall_survival_curves = []
        progression_free_survival_curves = []
        for f in filters:
            curves = await get_curve(f, gen3_graphql_client, access_token=access_token)
            if not curves:
                continue

            overall_survival_curve = curves.get("overall_survival")
            if overall_survival_curve:
                overall_survival_curves.append(overall_survival_curve)

            progression_free_survival_curve = curves.get("progression_free_survival")
            if progression_free_survival_curve:
                progression_free_survival_curves.append(progression_free_survival_curve)

        statistics = calculate_survival_statistics(overall_survival_curves)
        progression_free_survival_statistics = calculate_survival_statistics(
            progression_free_survival_curves
        )

        results = [
            {"meta": curve["meta"], "donors": curve["donors"]}
            for curve in overall_survival_curves
        ]
        progression_free_survival_results = [
            {"meta": curve["meta"], "donors": curve["donors"]}
            for curve in progression_free_survival_curves
        ]

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "results": results,
                "overallStats": statistics,
                "progressionFreeSurvival": {
                    "results": progression_free_survival_results,
                    "overallStats": progression_free_survival_statistics,
                },
            },
        )

    except ValueError as e:
        logger.error(f"Error while processing survival plot: {e}")
        raise HTTPException(status_code=500, detail="Error with survival calculation")
    except Exception as e:
        logger.error(f"Error while processing survival plot: {e}")
        raise HTTPException(status_code=500)


# Define a Pydantic model for the request body
class CompareSurvivalRequest(BaseModel):
    filters: List[Dict]
    doc_type: Optional[str] = Field(
        default=settings.case_centric_gql, description="set the index for case queries"
    )
    field: str
    limit: int = settings.MAX_CASES
    mode: Optional[str] = Field(
        default="intersection",
        description="set the mode for the plot. modes are: intersection, compare, s0_minus_s1, s1_minus_s0",
    )


@survival.post(
    path="/compare",
    dependencies=[Depends(get_guppy_client)],
    status_code=status.HTTP_200_OK,
    description="Retrieves the comparison survival plot(s) for the given pair of filters.",
    summary="Survival plot comparing two cohorts",
    responses={
        status.HTTP_200_OK: {"description": "Successfully processed the survival plot"},
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
async def compare(
    request: CompareSurvivalRequest,
    access_token: Optional[str] = Cookie(None),
    gen3_graphql_client: GuppyGQLClient = Depends(get_guppy_client),
    auth: Auth = Depends(Auth),
) -> JSONResponse:
    filters = request.filters
    field = request.field
    limit = request.limit
    doc_type = request.doc_type
    mode = request.mode

    if len(filters) != 2:
        raise HTTPException(
            status_code=400, detail="filters must be a list of 2 filters"
        )

    # get a list of cases to perform set operation on, for each cohort
    plot_items_0 = await cases.get_item_ids(
        gen3_graphql_client,
        doc_type,
        [field],
        filters[0],
        limit=min(limit, settings.MAX_CASES),
        access_token=access_token,
    )

    plot_items_1 = await cases.get_item_ids(
        gen3_graphql_client,
        doc_type,
        [field],
        filters[1],
        limit=limit,
        access_token=access_token,
    )

    if plot_items_0.get("data") is None:
        raise HTTPException(
            status_code=400, detail="No cases found for the first filter"
        )
    if plot_items_1.get("data") is None:
        raise HTTPException(
            status_code=400, detail="No cases found for the second filter"
        )

    # extract ids

    ids = glom(plot_items_0, f"data.{doc_type}", default=[])
    ids_0 = [x[field] for x in ids if x.get(field) is not None]

    ids = glom(plot_items_1, f"data.{doc_type}", default=[])
    ids_1 = [x[field] for x in ids if x.get(field) is not None]

    # covert to sets
    set0 = set(ids_0)
    set1 = set(ids_1)

    if mode == "compare":
        filter_0 = {"in": {field: ids_0}}
        filter_1 = {"in": {field: ids_1}}
        return await plot(
            PlotRequest(filters=[filter_0, filter_1]),
            access_token,
            gen3_graphql_client,
        )

    if mode == "s0_minus_s1":
        diff = list(set0 - set1)
        filter_0 = {"in": {field: diff}}
        filter_1 = {"in": {field: ids_1}}
        return await plot(
            PlotRequest(filters=[filter_0, filter_1]),
            access_token,
            gen3_graphql_client,
        )

    if mode == "s1_minus_s0":
        diff = list(set1 - set0)
        filter_0 = {"in": {field: ids_0}}
        filter_1 = {"in": {field: diff}}
        return await plot(
            PlotRequest(filters=[filter_0, filter_1]),
            access_token,
            gen3_graphql_client,
        )

    intersection = set0 & set1

    # Subtract the intersection from both sets
    item_id_0_minus_intersection = list(set0 - intersection)
    item_id_1_minus_intersection = list(set1 - intersection)

    # build graphql filter for both using in

    filter_0 = {"in": {field: item_id_0_minus_intersection}}
    filter_1 = {"in": {field: item_id_1_minus_intersection}}

    return await plot(
        PlotRequest(filters=[filter_0, filter_1]),
        access_token,
        gen3_graphql_client,
    )


class GenomicSurvivalRequest(BaseModel):
    case_filter: Dict
    filter: Dict
    symbol: str = Field(description="symbol to compare")
    limit: int = settings.MAX_CASES
    type: Optional[str] = Field(
        default="gene", description="set the type of plot gene or ssm"
    )


@survival.post(
    path="/compare_genomic",
    dependencies=[Depends(get_guppy_client)],
    status_code=status.HTTP_200_OK,
    description="Retrieves the comparison survival plot(s) for the given pair of filters.",
    summary="Survival plot comparing two cohorts",
    responses={
        status.HTTP_200_OK: {"description": "Successfully processed the survival plot"},
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
async def compare_genomic(
    request: GenomicSurvivalRequest,
    access_token: Optional[str] = Cookie(None),
    gen3_graphql_client: GuppyGQLClient = Depends(get_guppy_client),
    auth: Auth = Depends(Auth),
) -> JSONResponse:
    case_filter = request.case_filter
    fltr = request.filter
    limit = request.limit
    symbol = request.symbol
    plot_type = request.type

    # get all cases
    case_ids = await cases.get_item_ids(
        gen3_graphql_client,
        settings.case_centric_gql,
        ["case_id"],
        case_filter,
        limit=min(limit, settings.MAX_CASES),
        access_token=access_token,
    )

    case_id_list = list(
        set(case["case_id"] for case in case_ids["data"][settings.case_centric_gql])
    )

    genomic_filter = parse_gql_filter(fltr)
    [with_gene_query, without_gene_query] = genomic_survival_comparison_query(
        case_ids=case_id_list,
        genomic_filter=genomic_filter,
        genomic_id=symbol,
        mode=plot_type,
    )
    with_cases = {"in": {"case_id": with_gene_query}}
    without_cases = {"in": {"case_id": without_gene_query}}

    # TODO: the genomic_survival_comparison_query should be able to
    #  get the information needed for the survival plot without
    #  executing another query
    return await plot(
        PlotRequest(filters=[without_cases, with_cases]),
        access_token,
        gen3_graphql_client,
    )
