"""Optional real ES 7 integration: VISIBILITY_TEST_ES_URL=http://localhost:59200."""

import os
import uuid

import pytest
from elasticsearch import Elasticsearch
from elasticsearch_dsl import Search

from gen3analysis.file_visibility import (
    VisibilityElasticsearch,
    request_visibility_resources,
)
from gen3analysis.settings import settings

URL = os.getenv("VISIBILITY_TEST_ES_URL")
pytestmark = pytest.mark.skipif(
    not URL, reason="Set VISIBILITY_TEST_ES_URL to a disposable ES 7 cluster"
)
A, B = "/programs/MMRF/projects/private-a", "/programs/MMRF/projects/private-b"


@pytest.fixture
def cluster(monkeypatch):
    monkeypatch.setattr(settings, "FILE_VISIBILITY_ENABLED", True)
    admin = Elasticsearch(URL)
    es = VisibilityElasticsearch(URL)
    index = "visibility-test-" + uuid.uuid4().hex
    admin.indices.create(
        index=index,
        body={
            "mappings": {
                "properties": {
                    "file_id": {"type": "keyword"},
                    "category": {"type": "keyword"},
                    "_gen3_visibility": {"type": "keyword"},
                    "_gen3_visibility_authz": {"type": "keyword"},
                }
            }
        },
    )
    documents = [
        {"file_id": "legacy", "category": "public"},
        {"file_id": "public", "category": "public", "_gen3_visibility": "public"},
        {
            "file_id": "private-a",
            "category": "secret",
            "_gen3_visibility": "restricted",
            "_gen3_visibility_authz": [A],
        },
        {
            "file_id": "private-ab",
            "category": "secret",
            "_gen3_visibility": "restricted",
            "_gen3_visibility_authz": [A, B],
        },
        {"file_id": "invalid", "category": "secret", "_gen3_visibility": "restricted"},
    ]
    for document in documents:
        admin.index(index=index, id=document["file_id"], body=document)
    admin.indices.refresh(index=index)
    try:
        yield es, index
    finally:
        admin.indices.delete(index=index)
        es.close()
        admin.close()


@pytest.mark.parametrize("resources,expected", [((), 1), ((A,), 2), ((A, B), 3)])
def test_results_counts_facets_dsl_and_pit(cluster, resources, expected):
    es, index = cluster
    context = request_visibility_resources.set(resources)
    try:
        result = es.search(
            index=index,
            body={
                "_source": ["file_id"],
                "aggs": {"categories": {"terms": {"field": "category"}}},
            },
        )
        assert result["hits"]["total"]["value"] == expected
        assert (
            sum(
                bucket["doc_count"]
                for bucket in result["aggregations"]["categories"]["buckets"]
            )
            == expected
        )
        assert all(set(hit["_source"]) == {"file_id"} for hit in result["hits"]["hits"])
        assert es.count(index=index)["count"] == expected
        assert Search(using=es, index=index).count() == expected
        assert (
            len(Search(using=es, index=index).params(request_timeout=10).execute())
            == expected
        )
        pit = es.open_point_in_time(index=index, keep_alive="1m")["id"]
        found, after = [], None
        try:
            while True:
                body = {
                    "pit": {"id": pit, "keep_alive": "1m"},
                    "sort": ["_shard_doc"],
                    "size": 1,
                    "_source": ["file_id"],
                }
                if after:
                    body["search_after"] = after
                page = es.search(body=body)
                pit = page.get("pit_id", pit)
                hits = page["hits"]["hits"]
                if not hits:
                    break
                found.append(hits[0]["_source"]["file_id"])
                after = hits[-1]["sort"]
            assert len(found) == expected
        finally:
            es.close_point_in_time(body={"id": pit})
    finally:
        request_visibility_resources.reset(context)


def test_unmarked_index_is_hidden_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "FILE_VISIBILITY_ENABLED", True)
    admin, es = Elasticsearch(URL), VisibilityElasticsearch(URL)
    index = "visibility-legacy-" + uuid.uuid4().hex
    admin.index(index=index, body={"file_id": "legacy"}, refresh=True)
    context = request_visibility_resources.set((A,))
    try:
        assert es.count(index=index)["count"] == 0
    finally:
        request_visibility_resources.reset(context)
        admin.indices.delete(index=index)
        es.close()
        admin.close()


def test_more_than_1024_permissions_and_no_hidden_bucket_keys(cluster):
    es, index = cluster
    resources = tuple("/large/" + str(i) for i in range(1100)) + (A,)
    context = request_visibility_resources.set(resources)
    try:
        result = es.search(
            index=index, body={"aggs": {"ids": {"terms": {"field": "file_id"}}}}
        )
        assert {hit["_source"]["file_id"] for hit in result["hits"]["hits"]} == {
            "public",
            "private-a",
        }
        assert {
            bucket["key"] for bucket in result["aggregations"]["ids"]["buckets"]
        } == {"public", "private-a"}
        for value in (0, "0", 0.5, "0.99999999999999999", "9.9999999999999999e-1"):
            with pytest.raises(ValueError):
                es.search(
                    index=index,
                    body={
                        "aggs": {
                            "ids": {
                                "terms": {"field": "file_id", "min_doc_count": value}
                            }
                        }
                    },
                )
    finally:
        request_visibility_resources.reset(context)
