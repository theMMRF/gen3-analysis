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
    monkeypatch.setattr(settings, "PROJECT_VISIBILITY_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "PROJECT_VISIBILITY_CURSOR_KEY",
        "test-key-shared-across-workers-32bytes",
    )
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
                    "_gen3_file_visibility_version": {"type": "integer"},
                    "_gen3_file_authz": {"type": "keyword"},
                }
            }
        },
    )
    documents = [
        {"file_id": "legacy", "category": "public"},
        {
            "file_id": "public",
            "category": "public",
            "_gen3_file_visibility_version": 1,
            "_gen3_file_authz": ["/open"],
        },
        {
            "file_id": "private-a",
            "category": "secret",
            "_gen3_file_visibility_version": 1,
            "_gen3_file_authz": [A],
        },
        {
            "file_id": "private-ab",
            "category": "secret",
            "_gen3_file_visibility_version": 1,
            "_gen3_file_authz": [A, B],
        },
        {
            "file_id": "invalid",
            "category": "secret",
            "_gen3_file_visibility_version": 1,
        },
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


@pytest.mark.parametrize(
    "resources,expected",
    [((), 0), (("/open",), 1), (("/open", A), 2), (("/open", A, B), 3)],
)
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
        es = VisibilityElasticsearch(URL)  # Next HTTP request can use a different worker.
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
    monkeypatch.setattr(settings, "PROJECT_VISIBILITY_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "PROJECT_VISIBILITY_CURSOR_KEY",
        "test-key-shared-across-workers-32bytes",
    )
    admin, es = Elasticsearch(URL), VisibilityElasticsearch(URL)
    index = "visibility-legacy-" + uuid.uuid4().hex
    admin.index(index=index, body={"file_id": "legacy"}, refresh=True)
    context = request_visibility_resources.set((A,))
    try:
        with pytest.raises(ValueError, match="prepared"):
            es.count(index=index)
    finally:
        request_visibility_resources.reset(context)
        admin.indices.delete(index=index)
        es.close()
        admin.close()


def test_more_than_1024_permissions_and_no_hidden_bucket_keys(cluster):
    es, index = cluster
    resources = tuple("/large/" + str(i) for i in range(1100)) + (
        "/open",
        A,
    )
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


def test_global_facets_ignore_query_but_not_visibility(cluster):
    es, index = cluster
    context = request_visibility_resources.set(("/open",))
    try:
        result = es.search(
            index=index,
            body={
                "query": {"match_none": {}},
                "aggs": {
                    "all": {
                        "global": {},
                        "aggs": {"ids": {"terms": {"field": "file_id"}}},
                    }
                },
            },
        )
        assert result["hits"]["total"]["value"] == 0
        assert result["aggregations"]["all"]["doc_count"] == 1
        assert result["aggregations"]["all"]["ids"]["buckets"] == [
            {"key": "public", "doc_count": 1}
        ]
    finally:
        request_visibility_resources.reset(context)


def test_shared_cases_nested_queries_facets_and_pit(monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_VISIBILITY_ENABLED", True)
    monkeypatch.setattr(
        settings,
        "PROJECT_VISIBILITY_CURSOR_KEY",
        "test-key-shared-across-workers-32bytes",
    )
    admin, es = Elasticsearch(URL), VisibilityElasticsearch(URL)
    index = "visibility-case-" + uuid.uuid4().hex
    properties = {
        "case_id": {"type": "keyword"},
        "_gen3_file_visibility_version": {"type": "integer"},
        "_gen3_file_authz": {"type": "keyword"},
        "_gen3_file_summary": {"type": "object", "enabled": False},
        "files": {
            "type": "nested",
            "properties": {
                "file_id": {"type": "keyword"},
                "file_name": {"type": "keyword"},
                "_gen3_file_authz": {"type": "keyword"},
            },
        },
    }
    admin.indices.create(index=index, body={"mappings": {"properties": properties}})
    admin.index(
        index=index,
        id="case-1",
        refresh=True,
        body={
            "case_id": "case-1",
            "_gen3_file_visibility_version": 1,
            "files": [
                {
                    "file_id": "public",
                    "file_name": "rna.txt",
                    "_gen3_file_authz": ["/open"],
                },
                {
                    "file_id": "secret",
                    "file_name": "methylation.txt",
                    "_gen3_file_authz": [A],
                },
            ],
            "summary": {"file_count": 2, "file_size": 109},
            "_gen3_file_summary": [
                {"authz": ["/open"], "file_count": 1, "file_size": 9},
                {"authz": [A], "file_count": 1, "file_size": 100},
            ],
        },
    )
    context = request_visibility_resources.set(("/open",))
    try:
        result = es.search(
            index=index,
            body={
                "_source": [
                    "case_id",
                    "files.file_name",
                    "summary.file_count",
                    "summary.file_size",
                ]
            },
        )
        assert result["hits"]["hits"][0]["_source"] == {
            "case_id": "case-1",
            "files": [{"file_name": "rna.txt"}],
            "summary": {"file_count": 1, "file_size": 9},
        }
        probe = {
            "nested": {
                "path": "files",
                "query": {"term": {"files.file_name": "methylation.txt"}},
            }
        }
        assert es.count(index=index, body={"query": probe})["count"] == 0
        result = es.search(
            index=index,
            body={
                "aggs": {
                    "probe": {"filter": probe},
                    "files": {
                        "nested": {"path": "files"},
                        "aggs": {
                            "names": {"terms": {"field": "files.file_name"}},
                            "examples": {
                                "top_hits": {"size": 10, "_source": ["file_name"]}
                            },
                        },
                    },
                }
            },
        )
        assert result["aggregations"]["probe"]["doc_count"] == 0
        assert result["aggregations"]["files"]["doc_count"] == 1
        assert result["aggregations"]["files"]["names"]["buckets"] == [
            {"key": "rna.txt", "doc_count": 1}
        ]
        assert "methylation" not in str(result)
        assert "secret" not in str(result)
        assert "_gen3_file_" not in str(result)
        pit = es.open_point_in_time(index=index, keep_alive="1m")["id"]
        es = VisibilityElasticsearch(URL)  # Next HTTP request can use a different worker.
        try:
            result = es.search(
                body={
                    "pit": {"id": pit, "keep_alive": "1m"},
                    "_source": ["files.file_name"],
                }
            )
            pit = result.get("pit_id", pit)
            assert result["hits"]["hits"][0]["_source"] == {
                "files": [{"file_name": "rna.txt"}]
            }
        finally:
            es.close_point_in_time(body={"id": pit})
    finally:
        request_visibility_resources.reset(context)
        admin.indices.delete(index=index)
        es.close()
        admin.close()
