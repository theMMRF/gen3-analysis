from typing import List

from elasticsearch_dsl import Q, Search

from gen3analysis.filters.es.convert_gql_to_elastic_search import (
    convert_gql_to_elastic_search,
)
from gen3analysis.filters.gen3GQLFilters import GQLFilter, get_gql_filter_contents
from gen3analysis.gen3.es_client import get_es
from gen3analysis.query_builders.utils.combine_nested import (
    combine_nested_queries_simple,
)
from gen3analysis.query_builders.utils.extract_ids import extract_ids_from_hit
from gen3analysis.settings import settings


def includes_overall_survival(survival_type) -> bool:
    survival_type_value = getattr(survival_type, "value", survival_type)
    return survival_type_value in {"overall", "both"}


def includes_progression_free_survival(survival_type) -> bool:
    survival_type_value = getattr(survival_type, "value", survival_type)
    return survival_type_value in {"pfs", "both"}


def build_overall_survival_eligibility_query():
    return Q(
        "bool",
        must=[
            Q("exists", field="demographic.vital_status", boost=0),
            Q(
                "bool",
                should=[
                    Q(
                        "range",
                        demographic__days_to_death={"gt": 0, "boost": 0},
                    ),
                    Q(
                        "nested",
                        path="diagnoses",
                        ignore_unmapped=True,
                        query=Q(
                            "range",
                            diagnoses__days_to_last_follow_up={
                                "gt": 0,
                                "boost": 0,
                            },
                        ),
                    ),
                ],
            ),
        ],
    )


def build_progression_free_survival_eligibility_query():
    return Q(
        "nested",
        path="outcomes",
        ignore_unmapped=True,
        query=Q(
            "range",
            outcomes__survival_time_pfs={
                "gt": 0,
                "boost": 0,
            },
        ),
    )


def build_survival_eligibility_query(survival_type):
    survival_filters = []

    if includes_overall_survival(survival_type):
        survival_filters.append(build_overall_survival_eligibility_query())

    if includes_progression_free_survival(survival_type):
        survival_filters.append(build_progression_free_survival_eligibility_query())

    return Q("bool", should=survival_filters, minimum_should_match=1)


def build_gene_survival_query(
    genomic_filters,
    genomic_id,
    exclude_gene,
    case_ids: List[str],
    mode: str = "gene",
    survival_type: str = "overall",
):
    genomic_es_filters = [
        convert_gql_to_elastic_search(gf, index=settings.ES_CASE_CENTRIC_INDEX, boost=0)
        for gf in genomic_filters
    ]

    symbol_query = Q("term", gene__symbol={"value": genomic_id, "boost": 0})
    gene_has_ssm = Q(
        "nested",
        path="gene.ssm",
        ignore_unmapped=True,
        query=Q("bool", must=[Q("exists", field="gene.ssm.ssm_id")]),
    )
    gene_has_cnv = Q(
        "nested",
        path="gene.cnv",
        ignore_unmapped=True,
        query=Q("bool", must=[Q("exists", field="gene.cnv.cnv_id")]),
    )
    gene_has_mutation = Q(
        "bool",
        should=[gene_has_ssm, gene_has_cnv],
        minimum_should_match=1,
    )
    if mode == "ssm":
        symbol_query = Q(
            "nested",
            path="gene.ssm",
            ignore_unmapped=True,
            query=Q(
                "bool",
                must=[Q("term", gene__ssm__ssm_id={"value": genomic_id, "boost": 0})],
            ),
        )
        gene_has_mutation = gene_has_ssm

    if not exclude_gene:
        genomic_es_filters.append(
            Q(
                "nested",
                path="gene",
                ignore_unmapped=True,
                query=Q(
                    "bool",
                    must=[symbol_query, gene_has_mutation],
                ),
            )
        )

    # Combine nested queries to find a single gene that satisfies all filters.
    combined_filters = combine_nested_queries_simple(genomic_es_filters)

    q = Q(
        "bool",
        must=[
            Q(
                "terms",
                case_id=case_ids,
                boost=0,
            ),
            *combined_filters,
            build_survival_eligibility_query(survival_type),
        ],
    )
    if exclude_gene:
        if mode == "ssm":
            q.must.append(Q("terms", available_variation_data=["ssm"], boost=0))
        else:
            q.must.append(
                Q("terms", available_variation_data=["ssm", "cnv"], boost=0)
            )
        q.must_not = [
            Q(
                "nested",
                path="gene",
                ignore_unmapped=True,
                query=Q(
                    "bool",
                    must=[symbol_query, gene_has_mutation],
                ),
            )
        ]

    return q


def genomic_survival_comparison_query(
    case_ids: List[str],
    genomic_id: str,
    genomic_filter: GQLFilter,
    mode="gene",
    survival_type: str = "overall",
):
    genomic_filter_contents = get_gql_filter_contents(genomic_filter)

    s = Search(using=get_es(), index=settings.ES_CASE_CENTRIC_INDEX)
    s = s.extra(track_total_hits=True)
    s = s.source(["_id"])
    s = s[0 : settings.MAX_CASES]
    excluded_query = s.query(
        build_gene_survival_query(
            genomic_filter_contents, genomic_id, True, case_ids, mode, survival_type
        )
    )
    included_query = s.query(
        build_gene_survival_query(
            genomic_filter_contents, genomic_id, False, case_ids, mode, survival_type
        )
    )

    included_results = included_query.execute()
    excluded_results = excluded_query.execute()
    included_case_ids = extract_ids_from_hit(included_results)
    excluded_case_ids = extract_ids_from_hit(excluded_results)
    return [included_case_ids, excluded_case_ids]
