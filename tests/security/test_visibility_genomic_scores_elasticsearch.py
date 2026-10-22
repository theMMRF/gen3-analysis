"""Scores are affected-case counts in the genomic chart and table queries."""

import os
import uuid

import pytest
from elasticsearch import Elasticsearch

from gen3analysis.file_visibility import VisibilityElasticsearch
from gen3analysis.query_builders.genomic import queries
from gen3analysis.settings import settings

URL = os.getenv("VISIBILITY_TEST_ES_URL")
pytestmark = pytest.mark.skipif(not URL, reason="Requires disposable Elasticsearch 7")


def test_visibility_preserves_genomic_ranking_and_chart_counts(monkeypatch):
    admin, es = Elasticsearch(URL), VisibilityElasticsearch(URL)
    index = "visibility-genomic-scores-" + uuid.uuid4().hex
    case_ids = ["case-1", "case-2", "case-3"]
    properties = {
        "_gen3_file_visibility_version": {"type": "integer"},
        "_gen3_file_authz": {"type": "keyword"},
        "symbol": {"type": "keyword"},
        "gene_id": {"type": "keyword"},
        "case": {
            "type": "nested",
            "properties": {
                "case_id": {"type": "keyword"},
                "project": {"properties": {"project_id": {"type": "keyword"}}},
                "ssm": {
                    "type": "nested",
                    "properties": {"ssm_id": {"type": "keyword"}},
                },
            },
        },
    }
    admin.indices.create(index=index, body={"mappings": {"properties": properties}})
    try:
        # Insertion order differs from frequency order so zeroing every score
        # changes both the ranking and the chart's affected-case values.
        for symbol, count in [("LOW", 1), ("HIGH", 3), ("MID", 2)]:
            admin.index(
                index=index,
                id=symbol,
                body={
                    "_gen3_file_visibility_version": 1,
                    "symbol": symbol,
                    "gene_id": symbol,
                    "case": [
                        {
                            "case_id": case_id,
                            "project": {"project_id": "MMRF-commpass"},
                            "ssm": [{"ssm_id": "mutation-" + case_id}],
                        }
                        for case_id in case_ids[:count]
                    ],
                },
            )
        admin.indices.refresh(index=index)
        monkeypatch.setattr(settings, "ES_GENE_CENTRIC_INDEX", index)
        monkeypatch.setattr(queries, "get_es", lambda: es)
        monkeypatch.setattr(queries, "query_case_ids", lambda _: case_ids)

        def results(enabled):
            monkeypatch.setattr(settings, "PROJECT_VISIBILITY_ENABLED", enabled)
            # Use the actual query and score-to-numCases calculation used by
            # /gene_frequency_chart, plus the table's actual score query.
            chart = queries.query_top_genes(None, None, None, size=3)
            table = es.search(
                index=index,
                body={
                    "query": queries.build_gene_query([], [], case_ids).to_dict(),
                    "_source": ["symbol"],
                    "track_scores": True,
                },
            )
            scores = [(hit["_id"], hit["_score"]) for hit in table["hits"]["hits"]]
            return chart, scores

        baseline = results(False)
        assert [row["symbol"] for row in baseline[0]["data"]] == ["HIGH", "MID", "LOW"]
        assert baseline[0]["data"][0]["numCases"] > 0
        assert baseline[1] == [("HIGH", 3.0), ("MID", 2.0), ("LOW", 1.0)]
        assert results(True) == baseline
    finally:
        admin.indices.delete(index=index)
        es.close()
        admin.close()
