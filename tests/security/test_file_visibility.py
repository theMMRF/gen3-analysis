import asyncio
from unittest.mock import patch

import pytest
from elasticsearch import Elasticsearch

from gen3analysis.file_visibility import (
    VisibilityElasticsearch,
    apply_visibility as apply_policy,
    redact_file_source,
    protected_file_paths,
    visibility_resources,
    request_visibility_resources,
)
from gen3analysis.settings import settings


def apply_visibility(body):
    return apply_policy(body, {"files": "files._gen3_file_authz"})


RESOURCE = "/programs/MMRF/projects/private"


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_VISIBILITY_ENABLED", True)
    monkeypatch.setattr(
        VisibilityElasticsearch,
        "_layout",
        lambda self, index: ([index], {"files": "files._gen3_file_authz"}),
    )


def test_independent_visibility_action_is_exact():
    assert visibility_resources(
        {
            RESOURCE: [{"service": "indexd", "method": "read-metadata"}],
            "download": [{"service": "fence", "method": "read-storage"}],
            "metadata": [{"service": "guppy", "method": "read"}],
            "wrong": [{"service": "fence", "method": "not-read-storage"}],
            "wildcard": [{"service": "*", "method": "*"}],
        }
    ) == (RESOURCE, "wildcard")
    with pytest.raises(ValueError):
        visibility_resources({"error": {}})


def test_wraps_search_and_count_below_dsl_and_projection():
    es = VisibilityElasticsearch(hosts=["http://localhost:59200"])
    body = {
        "query": {"term": {"file_id": "secret"}},
        "_source": ["file_name"],
        "aggs": {"total": {"value_count": {"field": "file_id"}}},
    }
    context = request_visibility_resources.set((RESOURCE,))
    try:
        with patch.object(Elasticsearch, "search", return_value={}) as search:
            es.search(body=body, index="files")
            sent = search.call_args.kwargs["body"]
            assert sent["query"]["bool"]["filter"][0] == body["query"]
            assert sent["query"]["bool"]["filter"][1]["bool"]["filter"][1]["bool"][
                "should"
            ][1]["bool"]["filter"][1]["script"]["script"]["params"]["allowed"] == {
                RESOURCE: True
            }
            assert sent["_source"] is True
        with patch.object(Elasticsearch, "count", return_value={}) as count:
            es.count(body={"query": body["query"]}, index="files")
            assert "filter" in count.call_args.kwargs["body"]["query"]["bool"]
    finally:
        request_visibility_resources.reset(context)
    assert body["query"] == {"term": {"file_id": "secret"}}


@pytest.mark.asyncio
async def test_parallel_users_do_not_share_permissions():
    async def query(resources):
        context = request_visibility_resources.set(resources)
        try:
            await asyncio.sleep(0)
            return apply_visibility({})
        finally:
            request_visibility_resources.reset(context)

    first, second = await asyncio.gather(query((RESOURCE,)), query(()))
    assert first != second
    assert (
        apply_visibility({})["query"]["bool"]["filter"][1]["bool"]["filter"][1]["bool"][
            "should"
        ][1]["bool"]["filter"][1]["script"]["script"]["params"]["allowed"]
        == {}
    )


def test_global_facets_retain_contract_with_filtered_children():
    result = apply_visibility(
        {
            "aggs": {
                "all": {
                    "global": {},
                    "aggs": {"names": {"terms": {"field": "file_name"}}},
                }
            }
        }
    )
    assert result["aggs"]["all"]["global"] == {}
    guard = result["aggs"]["all"]["aggs"]["__gen3_visible_files"]
    assert guard["filter"] == result["query"]["bool"]["filter"][1]
    assert guard["aggs"]["names"] == {"terms": {"field": "file_name"}}


def test_disabled_preserves_existing_query(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_VISIBILITY_ENABLED", False)
    body = {"query": {"match_all": {}}}
    assert apply_visibility(body) is body


@pytest.mark.parametrize(
    "body",
    [
        {"suggest": {"names": {"term": {"field": "file_name"}}}},
        {
            "runtime_mappings": {
                "_gen3_visibility": {"type": "keyword", "script": "emit('public')"}
            }
        },
        {"aggs": {"names": {"significant_terms": {"field": "file_name"}}}},
    ],
)
def test_unfiltered_background_or_policy_override_is_rejected(body):
    with pytest.raises(ValueError):
        apply_visibility(body)


@pytest.mark.parametrize(
    "body",
    [
        {"aggs": {}, "aggregations": {"leaked": {"global": {}}}},
        {"aggs": {"outer": {"aggs": {}, "aggregations": {"leaked": {"global": {}}}}}},
        {"aggs": {"identifiers": {"terms": {"field": "file_id", "min_doc_count": 0}}}},
        {
            "aggs": {
                "outer": {
                    "aggs": {
                        "identifiers": {
                            "terms": {"field": "file_id", "min_doc_count": 0}
                        }
                    }
                }
            }
        },
    ],
)
def test_aggregation_aliases_and_zero_count_terms_rejected(body):
    with pytest.raises(ValueError):
        apply_visibility(body)


@pytest.mark.parametrize(
    "value",
    [
        0,
        "0",
        0.5,
        "0.5",
        "0.99999999999999999",
        "9.9999999999999999e-1",
        ".99999999999999999",
        False,
        None,
        [],
        "invalid",
    ],
)
def test_unsafe_term_count_is_rejected_in_nested_alias(value):
    with pytest.raises(ValueError):
        apply_visibility(
            {
                "aggs": {},
                "aggregations": {
                    "outer": {
                        "aggs": {
                            "ids": {
                                "terms": {"field": "file_id", "min_doc_count": value}
                            }
                        }
                    }
                },
            }
        )


@pytest.mark.parametrize("value", [1, "1", 1.5, "1.00000000000000001", ".1e1", "1.5"])
def test_positive_term_count_remains_allowed(value):
    apply_visibility(
        {"aggs": {"ids": {"terms": {"field": "file_id", "min_doc_count": value}}}}
    )


def test_shared_case_redacts_private_files_and_recomputes_counts():
    source = {
        "case_id": "c1",
        "files": [
            {"file_id": "a", "file_name": "rna.txt", "_gen3_file_authz": [RESOURCE]},
            {
                "file_id": "b",
                "file_name": "methylation.txt",
                "_gen3_file_authz": ["/private/methylation"],
            },
        ],
        "summary": {"file_count": 2, "file_size": 109},
        "_gen3_file_summary": [
            {
                "authz": [RESOURCE],
                "file_count": 1,
                "file_size": 9,
                "data_category": ["RNA"],
                "experimental_strategy": [],
            },
            {
                "authz": ["/private/methylation"],
                "file_count": 1,
                "file_size": 100,
                "data_category": ["methylation"],
                "experimental_strategy": [],
            },
        ],
    }
    result = redact_file_source(source, (RESOURCE,))
    assert result["case_id"] == "c1"
    assert result["files"] == [{"file_id": "a", "file_name": "rna.txt"}]
    assert result["summary"]["file_count"] == 1
    assert result["summary"]["file_size"] == 9
    assert "methylation" not in str(result)
    assert "_gen3_file_" not in str(result)


def test_bypassing_parameters_and_unbound_pit_are_rejected():
    es = VisibilityElasticsearch(hosts=["http://localhost:59200"])
    with pytest.raises(ValueError):
        es.search(index="file", params={"q": "*"})
    with pytest.raises(ValueError):
        es.search(body={"pit": {"id": "unknown"}})


def test_precomputed_file_summary_filters_are_rejected():
    with pytest.raises(ValueError):
        apply_visibility({"aggs": {"count": {"sum": {"field": "summary.file_count"}}}})
    with pytest.raises(ValueError):
        apply_visibility({"query": {"range": {"summary.file_count": {"gt": 0}}}})
    apply_visibility({"_source": ["summary.file_count"]})
