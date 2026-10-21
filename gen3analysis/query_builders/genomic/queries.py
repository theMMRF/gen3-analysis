import json
import threading
from functools import lru_cache
from typing import Optional, List, Dict, Any, Iterable

from elasticsearch_dsl import Q, A, Search, Nested
from glom import glom, Path

from gen3analysis.filters.es.convert_gql_to_elastic_search import (
    convert_gql_to_elastic_search,
)
from gen3analysis.filters.es.query_builder import ESQueryBuilder
from gen3analysis.filters.gen3GQLFilters import (
    GQLFilter,
    get_gql_filter_contents,
    GQLIncludes,
)
from gen3analysis.gen3.es_client import get_es, get_nested_registry
from gen3analysis.settings import settings


def combine_nested_queries(filters):
    """Combine multiple nested queries on the same path into single nested queries"""
    nested_by_path = {}
    other_filters = []

    for f in filters:
        a = isinstance(f, Nested)
        b = hasattr(f, "path")
        if isinstance(f, Nested) and hasattr(f, "path"):
            path = f.path
            if path not in nested_by_path:
                nested_by_path[path] = []
            nested_by_path[path].append(f.query)
        else:
            other_filters.append(f)

    # Create combined nested queries
    for path, queries in nested_by_path.items():
        combined = Q(
            "nested", path=path, ignore_unmapped=True, query=Q("bool", must=queries)
        )
        other_filters.append(combined)

    return other_filters


def build_gen3_es_query(
    case_filter: GQLFilter,
    size: Optional[int] = 20,
) -> Dict[str, Any]:
    """
    Builds the GraphQL query for retrieving case IDs.

    This function constructs a GraphQL query with the specified filters, optional
    fields to include, and the maximum size of results. The query is intended to
    retrieve case IDs based on the provided parameters.

    Parameters:
    filters (GQLFilter): The filter conditions for the GraphQL query.
    include (Optional[Union[str, List[str]]]): The fields or list of fields to
        include in the query output. Defaults to None.
    size (Optional[int]): The maximum number of results to retrieve. Defaults
        to 20.

    Returns:
    Dict[str, Any]: A dictionary representation of the constructed GraphQL query.
    """
    qb = ESQueryBuilder().size(size)
    # filter cases
    if case_filter:
        qb.filter(case_filter)
    else:
        qb.filter(Q("match_all"))

    return qb.to_dict()


def build_top_genes_by_case_count(
    gene_path: str = "consequence.transcript",  # nested path
    gene_field: str = "consequence.transcript.gene_symbol.keyword",
    case_nested_path: str = "occurrence.case",
    case_id_field: str = "occurrence.case.case_id",
    case_filter: Optional[Q] = None,
    size: int = 20,
) -> Dict[str, Any]:
    """
    Returns an aggregation that:
      - scopes into gene_path (nested)
      - buckets by gene symbol
      - for each gene, counts unique case_ids from a *different* nested path
    NOTE: In ES 7.x, mixing nested scopes requires care. We compute the case
    cardinality by a *separate* nested agg chained under the gene bucket.
    """

    qb = ESQueryBuilder().size(0)

    # Optional global filter on cases or other fields
    if case_filter:
        qb.filter(case_filter)

    # Nested into genes -> terms by gene -> nested into cases -> cardinality of case_id
    qb.agg_nested("genes", gene_path).subagg_terms(
        "genes", "by_gene", field=gene_field, size=size, order={"_count": "desc"}
    )

    # Under each gene bucket, scope into cases and compute unique case count
    qb.s.aggs["genes"]["by_gene"].bucket("cases", A("nested", path=case_nested_path))
    qb.s.aggs["genes"]["by_gene"]["cases"].metric(
        "unique_cases", A("cardinality", field=case_id_field, precision_threshold=40000)
    )

    return qb.to_dict()


def build_cases_by_gene_terms_query(
    gene_path: str,
    gene_id_field: str,
    gene_ids: Iterable[str],
    return_size: int = 0,
) -> Dict[str, Any]:
    """
    Example: filter to a list of gene_ids in a nested path (terms) and return hits or aggs.
    """
    qb = ESQueryBuilder().size(return_size)
    qb.filter(ESQueryBuilder.nested_terms(gene_path, gene_id_field, gene_ids))
    return qb.to_dict()


def build_composite_by_ssm(
    size: int = 200,
    after_key: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Composite over ssm_id with a nested cardinality of unique cases.
    Use after_key from the previous response to get the next page.
    """
    qb = ESQueryBuilder().size(0)

    qb.agg_composite(
        "ssm_idAggs",
        sources=[{"ssm_id": {"terms": {"field": "ssm_id"}}}],
        size=size,
        after_key=after_key,
    )

    # For each bucket: nested into cases -> unique case count
    qb.s.aggs["ssm_idAggs"].bucket("occ", A("nested", path="occurrence.case"))
    qb.s.aggs["ssm_idAggs"]["occ"].metric(
        "unique_cases",
        A("cardinality", field="occurrence.case.case_id", precision_threshold=40000),
    )

    return qb.to_dict()


def build_ssm_gene_mutations(
    gene_ids: List[str], case_ids: List[str], filters: Dict[str, Any]
):
    consequence_must = [
        Q("terms", **{"consequence.transcript.gene.gene_id": gene_ids}, boost=0),
    ]
    if "is_cancer_gene_census" in filters:
        consequence_must.append(
            Q(
                "terms",
                **{
                    "consequence.transcript.gene.is_cancer_gene_census": filters[
                        "is_cancer_gene_census"
                    ]
                },
                boost=0,
            )
        )
    if "biotype" in filters:
        consequence_must.append(
            Q(
                "terms",
                **{"consequence.transcript.gene.biotype": filters["biotype"]},
                boost=0,
            )
        )
    if "vep_impact" in filters:
        consequence_must.append(
            Q(
                "terms",
                **{
                    "consequence.transcript.annotation.vep_impact": filters[
                        "vep_impact"
                    ]
                },
                boost=0,
            )
        )
    if "consequence_type" in filters:
        consequence_must.append(
            Q(
                "terms",
                **{
                    "consequence.transcript.consequence_type": filters[
                        "consequence_type"
                    ]
                },
                boost=0,
            )
        )
    if "sift_impact" in filters:
        consequence_must.append(
            Q(
                "terms",
                **{
                    "consequence.transcript.annotation.sift_impact": filters[
                        "sift_impact"
                    ]
                },
                boost=0,
            )
        )
    if "polyphen_impact" in filters:
        consequence_must.append(
            Q(
                "terms",
                **{
                    "consequence.transcript.annotation.polyphen_impact": filters[
                        "polyphen_impact"
                    ]
                },
                boost=0,
            )
        )

    # ---- build the Search ----
    s = Search(using=get_es(), index=settings.ES_SSM_CENTRIC_INDEX)

    # _source: false and size: 0
    s = s.source(False)
    s = s[:0]

    # bool/must with nested queries + a top-level terms filter
    occurrence_nested_q = Q(
        "nested",
        path="occurrence",
        ignore_unmapped=True,
        query=Q(
            "bool", must=[Q("terms", **{"occurrence.case.case_id": case_ids}, boost=0)]
        ),
    )

    consequence_nested_q = Q(
        "nested",
        path="consequence",
        ignore_unmapped=True,
        query=Q("bool", must=consequence_must),
    )

    query_filters = [occurrence_nested_q, consequence_nested_q]

    if "mutation_subtype" in filters:
        mutation_subtype_q = Q("terms", mutation_subtype=filters["mutation_subtype"])
        query_filters.append(mutation_subtype_q)

    s = s.query(Q("bool", must=query_filters))

    # ---- aggregations ----
    # nested agg on "consequence"
    consequence_agg = s.aggs.bucket("consequence", "nested", path="consequence")

    # filtered sub-agg with the same 'consequence' constraints
    filtered = consequence_agg.bucket(
        "consequence.transcript.gene.gene_id:filtered",
        "filter",
        Q("bool", must=consequence_must),
    )

    # terms agg on gene_id, with reverse_nested to jump back to root docs if needed
    terms_agg = filtered.bucket(
        "consequence.transcript.gene.gene_id",
        "terms",
        field="consequence.transcript.gene.gene_id",
        size=200,
    )

    # reverse_nested sub-agg
    terms_agg.bucket("rn", "reverse_nested")
    results = s.execute()

    data_path = Path(
        "aggregations",
        "consequence",
        "consequence.transcript.gene.gene_id:filtered",
        "consequence.transcript.gene.gene_id",
        "buckets",
    )
    mutation_subtype_counts = glom(results, data_path, default=[])
    ssm_by_gene_id = {}
    for subtype_count in mutation_subtype_counts:
        gene_id = subtype_count["key"]
        ssm_by_gene_id[gene_id] = glom(subtype_count, "rn.doc_count", default=0)

    return ssm_by_gene_id


def build_cnv_case_total_query(cohort_filter: GQLFilter, case_ids: List[str]):
    q = Q(
        "bool",
        must=[
            Q("terms", case__case_id=case_ids, boost=0),
            Q("nested", path="case", ignore_unmapped=True, query=Q()),
        ],
    )


def query_case_count(case_filters: List[GQLFilter]) -> int:
    must_query = []
    for x in case_filters:
        must_query.append(
            convert_gql_to_elastic_search(x, settings.ES_CASE_CENTRIC_INDEX, boost=0)
        )

    case_filters_cnv = Q("bool", must=must_query)
    cnv_cases_s = Search(using=get_es(), index=settings.ES_CASE_CENTRIC_INDEX)
    cnv_cases_s = cnv_cases_s.source(False)
    cnv_cases_s = cnv_cases_s[:0]
    cnv_cases_s = cnv_cases_s.extra(track_total_hits=True)
    cnv_cases_s = cnv_cases_s.query(case_filters_cnv)
    results = cnv_cases_s.execute()
    return glom(results, "hits.total.value", default=0)


def build_cnv_change_query(gene_id: str, change: str, case_ids: List[str]):

    q = Q(
        "bool",
        must=[
            Q("ids", values=[gene_id], boost=0),
            Q(
                "nested",
                path="case",
                ignore_unmapped=True,
                query=Q(
                    "bool",
                    must=[
                        Q(
                            "terms",
                            case__case_id=case_ids,
                            boost=0,
                        ),
                        Q("terms", case__available_variation_data=["cnv"], boost=0),
                        Q(
                            "nested",
                            path="case.cnv",
                            ignore_unmapped=True,
                            query=Q(
                                "bool",
                                must=[
                                    Q(
                                        "terms",
                                        case__cnv__cnv_change_5_category=[change],
                                        boost=0,
                                    )
                                ],
                            ),
                        ),
                    ],
                ),
                inner_hits={
                    "size": 0,
                    "from": 0,
                    "_source": {"includes": ["case.case_id"]},
                },
            ),
        ],
    )
    return q


def build_total_case_count_for_gene_filters(gene_id: str):
    q = Q(
        "bool",
        must=[
            Q("ids", values=[gene_id], boost=0),
            Q(
                "nested",
                path="case",
                ignore_unmapped=True,
                query=Q(
                    "bool",
                    must=[
                        Q("terms", case__available_variation_data=["ssm"]),
                        Q(
                            "nested",
                            path="case.ssm",
                            ignore_unmapped=True,
                            query=Q(
                                "bool",
                                must=[
                                    Q(
                                        "nested",
                                        path="case.ssm.observation",
                                        ignore_unmapped=True,
                                        query=Q(
                                            "bool",
                                            must=[
                                                Q(
                                                    "exists",
                                                    field="case.ssm.observation.observation_id",
                                                    boost=0,
                                                )
                                            ],
                                        ),
                                    )
                                ],
                            ),
                        ),
                    ],
                ),
                inner_hits={
                    "size": 0,
                    "from": 0,
                    "_source": {"includes": ["case.case_id"]},
                },
            ),
        ],
    )
    return q


def build_gene_query(
    gene_es_filters: List[GQLFilter],
    ssm_es_filters: List[GQLFilter],
    case_ids: List[str],
    search: Optional[str] = None,
):
    # Build must-clauses conditionally
    must_clauses = [
        Q(
            "nested",
            path="case",
            ignore_unmapped=True,
            query=Q(
                "bool",
                must=[Q("terms", case__case_id=case_ids, boost=0), *ssm_es_filters],
            ),
        )
    ]
    if len(gene_es_filters) > 0:
        must_clauses.extend(gene_es_filters)

    if search is not None:
        must_clauses.append(
            Q("wildcard", symbol={"value": search, "case_insensitive": True})
        )

    top_gene_query = Q(
        "bool",
        filter=must_clauses,
        should=[
            Q(
                "bool",
                must=[
                    Q(
                        "nested",
                        path="case",
                        score_mode="sum",
                        query=Q(
                            "constant_score",
                            boost=1.0,
                            filter=Q(
                                "bool",
                                must=[
                                    Q(
                                        "bool",
                                        must=[
                                            Q(
                                                "bool",
                                                must=[
                                                    Q(
                                                        "terms",
                                                        case__case_id=case_ids,
                                                        boost=0,
                                                    ),
                                                    *ssm_es_filters,
                                                ],
                                            )
                                        ],
                                    ),
                                    Q(
                                        "bool",
                                        must=[
                                            Q(
                                                "nested",
                                                path="case.ssm",
                                                query=Q(
                                                    "exists", field="case.ssm.ssm_id"
                                                ),
                                            )
                                        ],
                                    ),
                                    Q("exists", field="case.project.project_id"),
                                ],
                                must_not=Q("term", case__project__project_id=""),
                            ),
                        ),
                    ),
                ],
            ),
            Q("bool", boost=0, must=Q("match_all")),
        ],
    )

    return top_gene_query


_case_id_cache: Dict[str, List[str]] = {}
_cache_lock = threading.Lock()
_in_flight: Dict[str, threading.Event] = {}

_REDIS_KEY_PREFIX = "gen3analysis:case_ids:"


@lru_cache(maxsize=1)
def _get_redis():
    """Return a Redis client if REDIS_URL is configured, else None."""
    if not settings.REDIS_URL:
        return None
    import redis

    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


def _make_case_filter_key(case_filter: Optional[GQLFilter]) -> str:
    if case_filter is None:
        return ""
    return case_filter.to_json()


def _redis_get(key: str) -> Optional[List[str]]:
    r = _get_redis()
    if r is None:
        return None
    raw = r.get(_REDIS_KEY_PREFIX + key)
    if raw is None:
        return None
    return json.loads(raw)


def _redis_set(key: str, case_ids: List[str]) -> None:
    r = _get_redis()
    if r is None:
        return
    r.set(_REDIS_KEY_PREFIX + key, json.dumps(case_ids), ex=settings.CASE_ID_CACHE_TTL)


def query_case_ids(case_filter: GQLFilter) -> List[str]:
    cache_key = _make_case_filter_key(case_filter)

    # Fast path: Redis (shared across all workers)
    cached = _redis_get(cache_key)
    if cached is not None:
        return cached

    # Coalescing: only one thread per worker fires the ES query for a given key
    while True:
        with _cache_lock:
            if cache_key in _case_id_cache:
                return _case_id_cache[cache_key]
            if cache_key not in _in_flight:
                event = threading.Event()
                _in_flight[cache_key] = event
                break
            event = _in_flight[cache_key]
        event.wait()
        # After waking, check Redis first — another worker may have populated it
        cached = _redis_get(cache_key)
        if cached is not None:
            return cached

    try:
        s = Search(using=get_es(), index=settings.ES_CASE_CENTRIC_INDEX)
        if case_filter:
            filters = convert_gql_to_elastic_search(
                case_filter, settings.ES_CASE_CENTRIC_INDEX
            )
        else:
            filters = Q("match_all")
        s = s[0 : settings.MAX_CASES]  # Get all cases
        s = s.source(False)
        s = s.query(filters)

        results = s.execute()
        case_ids = [x._id for x in results["hits"]["hits"]]

        _redis_set(cache_key, case_ids)

        with _cache_lock:
            if len(_case_id_cache) >= settings.CASE_ID_CACHE_MAX_SIZE:
                _case_id_cache.clear()
            _case_id_cache[cache_key] = case_ids
        return case_ids
    finally:
        with _cache_lock:
            _in_flight.pop(cache_key, None)
        event.set()


def query_top_genes(
    case_filter: GQLFilter,
    gene_filter: GQLFilter,
    ssm_filter: GQLFilter,
    size: int = 20,
    offset: int = 0,
) -> Dict[str, Any]:
    case_ids = query_case_ids(case_filter)

    if len(case_ids) == 0:
        return {"data": [], "total": 0}

    gene_filter_contents = get_gql_filter_contents(gene_filter)
    gene_es_filters = []
    for x in gene_filter_contents:
        gene_query = convert_gql_to_elastic_search(
            x, index=settings.ES_GENE_CENTRIC_INDEX, boost=0
        )
        gene_es_filters.append(gene_query)

    ssm_filter_contents = get_gql_filter_contents(ssm_filter)
    ssm_es_filters = []
    for x in ssm_filter_contents:
        ssm_query = convert_gql_to_elastic_search(
            x, index=settings.ES_GENE_CENTRIC_INDEX, start_path_index=1, boost=0
        )
        ssm_es_filters.append(ssm_query)

    # Build must-clauses conditionally
    must_clauses = [
        Q(
            "nested",
            path="case",
            ignore_unmapped=True,
            query=Q(
                "bool",
                must=[Q("terms", case__case_id=case_ids, boost=0), *ssm_es_filters],
            ),
        )
    ]
    if len(gene_es_filters) > 0:
        must_clauses.extend(gene_es_filters)

    top_gene_query = Q(
        "bool",
        filter=must_clauses,
        should=[
            Q(
                "bool",
                must=[
                    Q(
                        "nested",
                        path="case",
                        score_mode="sum",
                        query=Q(
                            "constant_score",
                            boost=1.0,
                            filter=Q(
                                "bool",
                                must=[
                                    Q(
                                        "bool",
                                        must=[
                                            Q(
                                                "bool",
                                                must=[
                                                    Q(
                                                        "terms",
                                                        case__case_id=case_ids,
                                                        boost=0,
                                                    ),
                                                    *ssm_es_filters,
                                                ],
                                            )
                                        ],
                                    ),
                                    Q(
                                        "bool",
                                        must=[
                                            Q(
                                                "nested",
                                                path="case.ssm",
                                                query=Q(
                                                    "exists", field="case.ssm.ssm_id"
                                                ),
                                            )
                                        ],
                                    ),
                                    Q("exists", field="case.project.project_id"),
                                ],
                                must_not=Q("term", case__project__project_id=""),
                            ),
                        ),
                    ),
                ],
            ),
            Q("bool", boost=0, must=Q("match_all")),
        ],
    )

    # given case ids, get 20 top genes

    gene_s = Search(using=get_es(), index=settings.ES_GENE_CENTRIC_INDEX)
    gene_s = gene_s.source(
        ["symbol", "name", "biotype", "gene_id", "is_cancer_gene_census"]
    )
    gene_s = gene_s.query(top_gene_query)
    gene_s = gene_s[offset:size]
    gene_s = gene_s.extra(track_scores=True)
    gene_s = gene_s.extra(track_total_hits=True)

    results = gene_s.execute()

    hits = results["hits"]["hits"]._l_
    gene_info = []
    for hit in hits:
        x_score = hit.get("_score", 0)
        max_score = max(x_score, 0)
        info = dict(hit.get("_source", {}))
        info["numCases"] = max_score
        gene_info.append(info)
    return {
        "filteredCases": len(case_ids),
        "data": gene_info,
        "genesTotal": glom(results, "hits.total.value", default=-1),
    }


def query_top_ssm(
    case_filter: GQLFilter,
    gene_filter: GQLFilter,
    ssm_filter: GQLFilter,
    size: int = 20,
    offset: int = 0,
) -> Dict[str, Any]:
    # get all the cases in the cohort
    case_ids = query_case_ids(case_filter)

    if len(case_ids) == 0:
        return {"data": [], "total": 0}

    gene_filter_contents = get_gql_filter_contents(gene_filter)
    gene_es_filters = []
    for x in gene_filter_contents:
        gene_query = convert_gql_to_elastic_search(
            x, index=settings.ES_SSM_CENTRIC_INDEX, boost=0, start_path_index=1
        )
        gene_es_filters.append(gene_query)

    ssm_filter_contents = get_gql_filter_contents(ssm_filter)
    ssm_es_filters = []
    for x in ssm_filter_contents:
        ssm_query = convert_gql_to_elastic_search(
            x, index=settings.ES_SSM_CENTRIC_INDEX, start_path_index=1, boost=0
        )
        ssm_es_filters.append(ssm_query)

    # Build must-clauses conditionally
    must_clauses = [
        Q(
            "nested",
            path="occurrence",
            ignore_unmapped=True,
            query=Q(
                "bool",
                must=[Q("terms", occurrence__case__case_id=case_ids, boost=0)],
            ),
        ),
        Q(
            "nested",
            path="consequence",
            ignore_unmapped=True,
            query=Q(
                "bool",
                must=[*ssm_es_filters, *gene_es_filters],
            ),
        ),
    ]

    top_ssm_query = Q(
        "bool",
        must=must_clauses,
        should=[
            Q(
                "bool",
                must=[
                    Q(
                        "nested",
                        path="occurrence",
                        score_mode="sum",
                        query=Q(
                            "constant_score",
                            boost=1.0,
                            filter=Q(
                                "bool",
                                must=[
                                    Q(
                                        "bool",
                                        must=[
                                            Q(
                                                "terms",
                                                occurrence__case__case_id=case_ids,
                                                boost=0,
                                            ),
                                        ],
                                    ),
                                    Q(
                                        "exists",
                                        field="occurrence.case.project.project_id",
                                    ),
                                ],
                                must_not=Q(
                                    "term", occurrence__case__project__project_id=""
                                ),
                            ),
                        ),
                    ),
                ],
            ),
            Q("bool", boost=0, must=Q("match_all")),
        ],
    )

    # given case ids, get 20 top ssm

    ssm_s = Search(using=get_es(), index=settings.ES_SSM_CENTRIC_INDEX)
    ssm_s = ssm_s.source(
        [
            "id",
            "score",
            "genomic_dna_change",
            "mutation_subtype",
            "ssm_id",
            "consequence",
        ]
    )
    ssm_s = ssm_s.query(top_ssm_query)
    ssm_s = ssm_s[offset:size]
    ssm_s = ssm_s.extra(track_scores=True)
    ssm_s = ssm_s.extra(track_total_hits=True)

    results = ssm_s.execute()

    hits = results["hits"]["hits"]._l_
    ssm_info = []
    for hit in hits:

        info = dict(hit.get("_source", {}))
        info["numCases"] = hit.get("_score", -1)
        con = glom(info, "consequence", default=[{}])
        info["consequence"] = con[0]
        ssm_info.append(info)
    return {
        "filteredCases": len(case_ids),
        "data": ssm_info,
        "mutationsTotal": glom(results, "hits.total.value", default=-1),
    }


def gene_table_query(
    case_filter: GQLFilter,
    gene_filter: GQLFilter,
    ssm_filter: GQLFilter,
    size: int = 20,
    offset: int = 0,
    search: Optional[str] = None,
) -> Dict[str, Any]:

    case_ids = query_case_ids(case_filter)

    # get SSM cases
    case_filter_contents = get_gql_filter_contents(case_filter)

    case_filter_contents.append(GQLIncludes({"available_variation_data": ["ssm"]}))
    must_query = []
    for x in case_filter_contents:
        must_query.append(
            convert_gql_to_elastic_search(x, settings.ES_CASE_CENTRIC_INDEX, boost=0)
        )

    case_filters_ssm = Q("bool", must=must_query)
    ssm_cases_s = Search(using=get_es(), index=settings.ES_CASE_CENTRIC_INDEX)
    ssm_cases_s = ssm_cases_s.source(False)
    ssm_cases_s = ssm_cases_s[:0]
    ssm_cases_s = ssm_cases_s.extra(track_total_hits=True)
    ssm_cases_s = ssm_cases_s.query(case_filters_ssm)
    results = ssm_cases_s.execute()

    ssm_case_total = glom(results, "hits.total.value", default=0)

    # get CNV cases, apply the cohort filters, gene and ssm filters
    case_filter_contents = get_gql_filter_contents(case_filter)

    case_filter_contents.append(GQLIncludes({"available_variation_data": ["cnv"]}))
    must_query = []
    for x in case_filter_contents:
        must_query.append(
            convert_gql_to_elastic_search(x, settings.ES_CASE_CENTRIC_INDEX, boost=0)
        )

    case_filters_cnv = Q("bool", must=must_query)
    cnv_cases_s = Search(using=get_es(), index=settings.ES_CASE_CENTRIC_INDEX)
    cnv_cases_s = cnv_cases_s.source(False)
    cnv_cases_s = cnv_cases_s[:0]
    cnv_cases_s = cnv_cases_s.extra(track_total_hits=True)
    cnv_cases_s = cnv_cases_s.query(case_filters_cnv)
    results = cnv_cases_s.execute()

    cnv_case_total = glom(results, "hits.total.value", default=0)

    # get the genes counts for the current size and offset

    # set up the gene and ssm filters
    gene_filter_contents = get_gql_filter_contents(gene_filter)
    gene_es_filters = []
    for x in gene_filter_contents:
        gene_query = convert_gql_to_elastic_search(
            x, index=settings.ES_GENE_CENTRIC_INDEX, boost=0
        )
        gene_es_filters.append(gene_query)

    # add case.ssm to the ssm filters
    ssm_filter_contents = get_gql_filter_contents(ssm_filter)
    ssm_es_filters = []
    for x in ssm_filter_contents:
        ssm_query = convert_gql_to_elastic_search(
            x, index=settings.ES_GENE_CENTRIC_INDEX, start_path_index=1, boost=0
        )
        ssm_es_filters.append(ssm_query)

    # then get the top genes
    gene_cases_s = Search(using=get_es(), index=settings.ES_GENE_CENTRIC_INDEX)
    gene_cases_s = gene_cases_s.source(
        [
            "score",
            "symbol",
            "name",
            "cytoband",
            "biotype",
            "gene_id",
            "is_cancer_gene_census",
        ]
    )

    genes_by_cases_query = build_gene_query(
        gene_es_filters, ssm_es_filters, case_ids, search
    )
    gene_cases_s = gene_cases_s.query(genes_by_cases_query)
    gene_cases_s = gene_cases_s[offset:size]
    gene_cases_s = gene_cases_s.extra(track_scores=True)
    gene_cases_s = gene_cases_s.extra(track_total_hits=True)

    results = gene_cases_s.execute()

    total_genes_count = glom(results, "hits.total.value", default=0)
    # now we have the list of genes, create multiple queries for each gene for cnv and ssm counts
    gene_information = {}
    gene_ids = []
    for x in results["hits"]["hits"]:

        x_score = glom(x, "_score", default=0)
        max_score = max(x_score, 0)
        gene_information[x._id] = {
            "gene_id": x._id,
            "id": x._id,  # to be compatible with GDC response
            # The nested constant-score query contributes one per matching case.
            "case_count": max_score,
            **(x._source.to_dict()),
        }
        gene_ids.append(x._id)

    for gene_id in gene_ids:
        gene_all_cases_s = Search(using=get_es(), index=settings.ES_GENE_CENTRIC_INDEX)
        gene_all_cases_s = gene_all_cases_s[:1]
        gene_all_cases_s = gene_all_cases_s.extra(track_scores=False)
        gene_all_cases_s = gene_all_cases_s.source(False)
        gene_count_all_cases_query = build_total_case_count_for_gene_filters(gene_id)
        gene_all_cases_s = gene_all_cases_s.query(gene_count_all_cases_query)
        results = gene_all_cases_s.execute()
        base = glom(results, "hits.hits", default={"_l_": [None]})[0]
        total = glom(base, "inner_hits.case.hits.total.value", default=0)
        gene_information[gene_id]["ssm_cases_across_commons"] = total

        # get the cnv count for each change type
        for change in [
            "Gain",
            "Loss",
            "Neutral",
            "Amplification",
            "Homozygous Deletion",
        ]:
            cnv_s = Search(using=get_es(), index=settings.ES_GENE_CENTRIC_INDEX)
            cnv_s = cnv_s[:1]
            cnv_s = cnv_s.extra(track_scores=False)
            cnv_s = cnv_s.source(False)
            cnv_query = build_cnv_change_query(gene_id, change, case_ids)
            cnv_s = cnv_s.query(cnv_query)

            results = cnv_s.execute()
            base = glom(results, "hits.hits", default=[{"novalue": True}])
            if len(base) == 0:
                gene_information[gene_id][
                    f"cnv_count_{change.lower().replace(' ', '_')}"
                ] = 0
            else:
                base_array = base[0]
                total = glom(base_array, "inner_hits.case.hits.total.value", default=0)
                gene_information[gene_id][
                    f"cnv_count_{change.lower().replace(' ', '_')}"
                ] = total

    # get the ssm mutations and counts
    # build the filters from the gene and ssm filter list
    filters = {}
    for gf in gene_filter_contents:
        for gene_filter_key in ["is_cancer_gene_census", "biotype"]:
            if gf.search(gene_filter_key):
                filters[gene_filter_key] = gf.get_values()
    for sf in ssm_filter_contents:
        for ssm_filter_key in [
            "vep_impact",
            "consequence_type",
            "sift_impact",
            "polyphen_impact",
            "subtype",
        ]:
            if sf.search(ssm_filter_key):
                filters[ssm_filter_key] = sf.get_values()

    ssm_counts = build_ssm_gene_mutations(gene_ids, case_ids, filters)
    for key in ssm_counts:
        if key in gene_information:
            gene_information[key]["ssm_count"] = ssm_counts[key]
    return {
        "cnvCases": cnv_case_total,
        "totalCases": len(case_ids),
        "ssmCases": ssm_case_total,
        "genesTotal": total_genes_count,
        "genes": [x for x in gene_information.values()],
    }


def gene_caseids_query(
    case_filter: GQLFilter,
    gene_filter: GQLFilter,
    ssm_filter: GQLFilter,
) -> Search:

    case_ids = query_case_ids(case_filter)
    # The case_id filter used in both must and scoring
    case_id_filter = Q("terms", **{"case.case_id": case_ids}, boost=0)

    gene_filter_contents = get_gql_filter_contents(gene_filter)
    gene_es_filters = []
    for x in gene_filter_contents:
        gene_query = convert_gql_to_elastic_search(
            x, index=settings.ES_GENE_CENTRIC_INDEX, boost=0, start_path_index=1
        )
        gene_es_filters.append(gene_query)

    ssm_filter_contents = get_gql_filter_contents(ssm_filter)
    ssm_es_filters = []
    for x in ssm_filter_contents:
        ssm_query = convert_gql_to_elastic_search(
            x, index=settings.ES_GENE_CENTRIC_INDEX, start_path_index=1, boost=0
        )
        ssm_es_filters.append(ssm_query)

    s = Search(using=get_es(), index=settings.ES_GENE_CENTRIC_INDEX)

    # Nested filter that defines "a case counts" - mirrored in both query and agg
    case_filter = Q(
        "nested",
        path="case",
        query=Q(
            "bool",
            filter=[
                Q("terms", **{"case.case_id": case_ids}),
                Q(
                    "nested",
                    path="case.ssm",
                    query=Q("match_all"),
                ),
            ],
        ),
    )

    s = s.filter(case_filter)
    # add the gene filters
    for f in gene_es_filters:
        s = s.filter(f)
    for f in ssm_es_filters:
        s = s.filter(f)
    s.aggs.bucket("cases", "nested", path="case").bucket(
        "filtered",
        "filter",
        filter=Q(
            "bool",
            filter=[
                Q("terms", **{"case.case_id": case_ids}),
                Q(
                    "nested",
                    path="case.ssm",
                    query=Q("match_all"),
                ),
            ],
        ),
    ).bucket(
        "case_ids",
        "terms",
        field="case.case_id",
        size=10000,
    )

    s = s.source([])
    s = s.extra(size=0)

    response = s.execute()

    buckets = response.aggregations.cases.filtered.case_ids.buckets

    return [bucket.key for bucket in buckets]
