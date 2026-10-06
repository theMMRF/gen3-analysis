import asyncio
from unittest.mock import patch

import pytest
from elasticsearch import Elasticsearch

from gen3analysis.file_visibility import (
    VisibilityElasticsearch,
    apply_visibility,
    visibility_resources,
    request_visibility_resources,
)
from gen3analysis.settings import settings

RESOURCE = "/programs/MMRF/projects/private"


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "FILE_VISIBILITY_ENABLED", True)


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
            assert sent["query"]["bool"]["filter"][1]["bool"]["should"][1]["bool"][
                "filter"
            ][2]["script"]["script"]["params"]["allowed"] == {RESOURCE: True}
            assert sent["_source"] == body["_source"]
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
        len(apply_visibility({})["query"]["bool"]["filter"][1]["bool"]["should"]) == 1
    )


def test_global_aggregation_cannot_bypass_policy():
    with pytest.raises(ValueError):
        apply_visibility({"aggs": {"outer": {"aggs": {"all": {"global": {}}}}}})


def test_disabled_preserves_existing_query(monkeypatch):
    monkeypatch.setattr(settings, "FILE_VISIBILITY_ENABLED", False)
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
