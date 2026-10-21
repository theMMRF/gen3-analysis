from typing import Optional, List, Dict, Any

from elasticsearch_dsl import Q, Search
from glom import glom, flatten


from gen3analysis.filters.es.convert_gql_to_elastic_search import (
    convert_gql_to_elastic_search,
)
from gen3analysis.filters.gen3GQLFilters import (
    GQLFilter,
    get_gql_filter_contents,
    GQLIncludes,
)

from gen3analysis.gen3.es_client import get_es
from gen3analysis.query_builders.genomic.queries import query_case_ids, query_case_count
from gen3analysis.settings import settings


def get_ssm_total_count_for_data_commons(
    ssm_id: str,
) -> Search:
    """
    Build an elasticsearch-dsl Search equivalent to the given ES v7 query:
      - _source: false
      - size: 1
      - track_scores: false
      - must: ids(ssm_id) AND nested(occurrence) with terms filter on available_variation_data == "ssm"
      - nested inner_hits with includes on occurrence.occurrence_id (size:0, from:0)
    """

    s = Search(using=get_es(), index=settings.ES_SSM_CENTRIC_INDEX)
    s = s.source(False)  # "_source": false
    s = s.extra(track_scores=False)
    s = s[:1]  # size: 1

    ids_q = Q("ids", values=[ssm_id])

    inner_hits = {
        "size": 0,
        "from": 0,
        "_source": {"includes": ["occurrence.occurrence_id"]},
    }

    occurrence_nested_q = Q(
        "nested",
        path="occurrence",
        ignore_unmapped=True,
        query=Q(
            "bool",
            must=[Q("terms", **{"occurrence.case.available_variation_data": ["ssm"]})],
        ),
        inner_hits=inner_hits,
    )

    s = s.query(Q("bool", must=[ids_q, occurrence_nested_q]))
    response = s.execute()
    total = glom(
        response,
        ("hits.hits._l_", ["inner_hits.occurrence.hits.total.value"]),
        default=0,
    )
    return total[0]


def ssm_canonical_information_query(
    ssm_id: str,
) -> Search:
    """
    Build the elasticsearch-dsl Search for the given ssm_id.
    - _source: false
    - size: 1
    - track_scores: false
    - nested filter on consequence.transcript.is_canonical == "true"
    - inner_hits on the nested path with selected fields
    """

    s = Search(using=get_es(), index=settings.ES_SSM_CENTRIC_INDEX)
    s = s.source(False)  # "_source": false
    s = s.extra(track_scores=False)
    s = s[:1]  # size: 1 (from defaults to 0)

    # ids query
    ids_q = Q("ids", values=[ssm_id])

    # nested filter + inner_hits
    inner_hits = {
        "size": 1,
        "from": 0,
        "_source": {
            "includes": [
                "consequence.consequence_id",
                "consequence.transcript.is_canonical",
                "consequence.transcript.annotation.vep_impact",
                "consequence.transcript.annotation.polyphen_impact",
                "consequence.transcript.annotation.polyphen_score",
                "consequence.transcript.annotation.sift_score",
                "consequence.transcript.annotation.sift_impact",
                "consequence.transcript.consequence_type",
                "consequence.transcript.gene.gene_id",
                "consequence.transcript.gene.symbol",
                "consequence.transcript.aa_change",
                "consequence.id",
            ]
        },
    }

    nested_q = Q(
        "nested",
        path="consequence",
        ignore_unmapped=True,
        query=Q(
            "bool",
            must=[Q("terms", **{"consequence.transcript.is_canonical": ["true"]})],
        ),
        inner_hits=inner_hits,
    )

    # combine
    s = s.query(Q("bool", must=[ids_q, nested_q]))
    resp = s.execute()
    return resp.to_dict()


def build_ssm_query(
    ssm_id: str,
    index: str,
) -> Search:
    """
    Build an elasticsearch-dsl Search that:
      - filters by _id == ssm_id via ids query
      - filters nested consequence docs to canonical transcripts
      - returns nested inner_hits with selected fields
      - sets _source:false on top hits, size:1, track_scores:false
    """

    s = Search(using=get_es(), index=index)
    s = s.source(False)  # "_source": false
    s = s.extra(track_scores=False)
    s = s[:1]  # size: 1 (from defaults to 0)

    # ids query with the given ssm_id
    ids_q = Q("ids", values=[ssm_id])

    # nested filter (canonical transcripts) + inner_hits
    inner_hits = {
        "size": 1,
        "from": 0,
        "_source": {
            "includes": [
                "consequence.consequence_id",
                "consequence.transcript.is_canonical",
                "consequence.transcript.annotation.vep_impact",
                "consequence.transcript.annotation.polyphen_impact",
                "consequence.transcript.annotation.polyphen_score",
                "consequence.transcript.annotation.sift_score",
                "consequence.transcript.annotation.sift_impact",
                "consequence.transcript.consequence_type",
                "consequence.transcript.gene.gene_id",
                "consequence.transcript.gene.symbol",
                "consequence.transcript.aa_change",
                "consequence.id",
            ]
        },
    }

    consequence_nested_q = Q(
        "nested",
        path="consequence",
        ignore_unmapped=True,
        query=Q(
            "bool",
            must=[Q("terms", **{"consequence.transcript.is_canonical": ["true"]})],
        ),
        inner_hits=inner_hits,
    )

    s = s.query(Q("bool", must=[ids_q, consequence_nested_q]))
    return s


def query_ssm_ids(
    case_ids: List[str], size: int, offset: int, filters: Dict[str, List[str]]
) -> Any:
    """
    Queries SSM IDs for a list of case IDds considering the filters and search filters
    """

    consequence_filters = []
    if "is_cancer_gene_census" in filters:
        consequence_filters.append(
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
        consequence_filters.append(
            Q(
                "terms",
                **{"consequence.transcript.gene.biotype": filters["biotype"]},
                boost=0,
            )
        )
    if "vep_impact" in filters:
        consequence_filters.append(
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
        consequence_filters.append(
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
        consequence_filters.append(
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
        consequence_filters.append(
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

    s = Search(using=get_es(), index=settings.ES_SSM_CENTRIC_INDEX)

    # _source includes + track_scores + pagination (from/size)
    s = s.source(
        [
            "id",
            "score",
            "genomic_dna_change",
            "mutation_subtype",
            "ssm_id",
            "consequence",
        ]
    )

    s = s.extra(track_scores=True)
    s = s[offset:size]  # from: 0, size: 10

    # ----- MUST: nested occurrence filter -----
    occurrence_nested = Q(
        "nested",
        path="occurrence",
        ignore_unmapped=True,
        query=Q(
            "bool", must=[Q("terms", **{"occurrence.case.case_id": case_ids}, boost=0)]
        ),
    )

    # ----- MUST: nested consequence filter -----
    consequence_nested = Q(
        "nested",
        path="consequence",
        ignore_unmapped=True,
        query=Q("bool", must=consequence_filters),
    )

    # Optional search string
    regex_should = Q(
        "bool",
        should=[
            Q(
                "nested",
                path="consequence",
                ignore_unmapped=True,
                query=Q(
                    "bool",
                    must=[
                        Q(
                            "regexp",
                            **{
                                "consequence.transcript.gene.gene_id": {
                                    "value": ".*.*",
                                    "boost": 0,
                                }
                            },
                        )
                    ],
                ),
            ),
            Q("regexp", **{"genomic_dna_change": {"value": ".*.*", "boost": 0}}),
            Q(
                "nested",
                path="consequence",
                ignore_unmapped=True,
                query=Q(
                    "bool",
                    must=[
                        Q(
                            "regexp",
                            **{
                                "consequence.transcript.gene.symbol": {
                                    "value": ".*.*",
                                    "boost": 0,
                                }
                            },
                        )
                    ],
                ),
            ),
            Q("regexp", **{"gene_aa_change": {"value": ".*.*", "boost": 0}}),
            Q("regexp", **{"ssm_id": {"value": ".*.*", "boost": 0}}),
        ],
    )

    scoring_should = Q(
        "bool",
        must=[
            Q(
                "nested",
                path="occurrence",
                score_mode="sum",
                query=Q(
                    "constant_score",
                    filter=Q(
                        "bool",
                        must=[
                            Q(
                                "bool",
                                must=[
                                    Q(
                                        "terms",
                                        **{"occurrence.case.case_id": case_ids},
                                        boost=0,
                                    )
                                ],
                            ),
                            Q("exists", field="occurrence.case.project.project_id"),
                        ],
                        must_not=Q(
                            "term", **{"occurrence.case.project.project_id": ""}
                        ),
                    ),
                    boost=1.0,
                ),
            )
        ],
    )

    # ----- SHOULD: match_all (with boost=0 in original; harmless either way) -----
    match_all_should = Q("bool", must=[Q("match_all")], boost=0)

    # ----- combine the query -----
    must_filters = [occurrence_nested, consequence_nested, regex_should]
    if "mutation_subtype" in filters:
        mutation_subtype_q = Q(
            "terms", mutation_subtype=filters["mutation_subtype"], boost=0
        )
        must_filters.append(mutation_subtype_q)

    s = s.query(
        Q(
            "bool",
            must=must_filters,
            should=[scoring_should, match_all_should],
        )
    )

    # ----- sorting -----
    s = s.sort(
        {"_score": {"order": "desc"}},
        {"_id": {"order": "asc", "mode": "min", "missing": "_last"}},
    )
    s = s.extra(track_scores=True)
    s = s.extra(track_total_hits=True)

    results = s.execute()

    total = glom(results, "hits.total.value", default=0)
    ssm_ids = []
    data_root = glom(results, "hits.hits._l_", default=[])
    for ssm in data_root:
        consequence = glom(ssm, "_source.consequence", default=[])
        ssm_ids.append(
            {
                "ssm_id": ssm["_id"],
                "score": max(ssm["_score"] - 1, 0),
                "mutation_subtype": glom(ssm, "_source.mutation_subtype", default=""),
                "genomic_dna_change": glom(
                    ssm, "_source.genomic_dna_change", default=""
                ),
                "consequence": consequence[:1],
            }
        )
    return {"ssmsTotal": total, "ssm_ids": ssm_ids}


def ssm_table_query(
    case_filter: GQLFilter,
    gene_filter: GQLFilter,
    ssm_filter: GQLFilter,
    size: int = 20,
    offset: int = 0,
    search: Optional[str] = ".*.*",
) -> Dict[str, Any]:

    # first get the case id using the cohort filter

    case_ids = query_case_ids(case_filter)

    if len(case_ids) == 0:
        return {"data": [], "total": 0}

    case_filter_with_ssm = get_gql_filter_contents(case_filter)
    case_filter_with_ssm.append(GQLIncludes({"available_variation_data": ["ssm"]}))

    gene_filter_contents = get_gql_filter_contents(gene_filter)
    gene_es_filters = []
    ssm_ids_filters = {}
    for gf in gene_filter_contents:
        gene_query = convert_gql_to_elastic_search(
            gf, index=settings.ES_SSM_CENTRIC_INDEX, boost=0
        )
        gene_es_filters.append(gene_query)
        for gene_filter_key in ["is_cancer_gene_census", "biotype"]:
            if gf.search(gene_filter_key):
                ssm_ids_filters[gene_filter_key] = gf.get_values()

    ssm_filter_contents = get_gql_filter_contents(ssm_filter)
    ssm_es_filters = []
    for sf in ssm_filter_contents:
        ssm_query = convert_gql_to_elastic_search(
            sf, index=settings.ES_SSM_CENTRIC_INDEX, start_path_index=0, boost=0
        )
        ssm_es_filters.append(ssm_query)
        for ssm_filter_key in [
            "consequence_type",
            "sift_impact",
            "polyphen_impact",
            "subtype",
            "vep_impact",
        ]:
            if sf.search(ssm_filter_key):
                ssm_ids_filters[ssm_filter_key] = sf.get_values()

    ssm_total = query_case_count(case_filter_with_ssm)

    ssm_ids = query_ssm_ids(case_ids, size, offset, ssm_ids_filters)
    results = []
    # for each id we need to get the information for canonical transcripts
    for ssm in ssm_ids["ssm_ids"]:
        ssm_id = ssm["ssm_id"]
        info = ssm_canonical_information_query(ssm_id)
        ssm_info = glom(
            info,
            (
                "hits.hits",
                [("inner_hits.consequence.hits.hits", ["_source.transcript"])],
            ),
        )
        ssm_info = flatten(ssm_info)
        ssm_total_count_commons = get_ssm_total_count_for_data_commons(ssm_id)
        results.append({**ssm, **ssm_info[0], "total_commons": ssm_total_count_commons})

    return {
        "filteredCases": ssm_total,
        "cases": len(case_ids),
        "ssmsTotal": ssm_ids.get("ssmsTotal", 0),
        "ssms": results,
    }
