"""Count recurring variants against an explicitly supplied disposable ES instance.

Set TEST_ES_ENDPOINT to run this integration test; no existing index is used.
"""

import os
import unittest
from uuid import uuid4
from unittest.mock import patch

from elasticsearch import Elasticsearch

from gen3analysis.query_builders.genomic import ssm


@unittest.skipUnless(os.environ.get("TEST_ES_ENDPOINT"), "requires disposable ES")
class RecurringMutationCounts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Create a fresh index with two variants shared by three cases."""
        cls.client = Elasticsearch(os.environ["TEST_ES_ENDPOINT"])
        cls.index = "test-ssm-counts-" + uuid4().hex
        cls.client.indices.create(
            index=cls.index,
            body={
                "mappings": {
                    "properties": {
                        "ssm_id": {"type": "keyword"},
                        "occurrence": {
                            "type": "nested",
                            "properties": {
                                "case": {
                                    "properties": {
                                        "case_id": {"type": "keyword"},
                                        "project": {
                                            "properties": {
                                                "project_id": {"type": "keyword"}
                                            }
                                        },
                                    }
                                },
                            },
                        },
                        "consequence": {
                            "type": "nested",
                            "properties": {
                                "transcript": {
                                    "properties": {
                                        "gene": {
                                            "properties": {
                                                "symbol": {"type": "keyword"},
                                                "biotype": {"type": "keyword"},
                                                "is_cancer_gene_census": {
                                                    "type": "keyword"
                                                },
                                            }
                                        },
                                    }
                                },
                            },
                        },
                    }
                }
            },
        )
        for variant, carriers, biotype in [
            ("shared", ["a", "b", "c"], "protein_coding"),
            ("rare", ["a"], "other"),
        ]:
            cls.client.index(
                index=cls.index,
                id=variant,
                body={
                    "ssm_id": variant,
                    "occurrence": [
                        {
                            "case": {
                                "case_id": case,
                                "project": {"project_id": "synthetic"},
                            }
                        }
                        for case in carriers
                    ],
                    "consequence": [
                        {
                            "transcript": {
                                "gene": {
                                    "symbol": "TEST",
                                    "biotype": biotype,
                                    "is_cancer_gene_census": "true",
                                }
                            }
                        }
                    ],
                },
            )
        cls.client.indices.refresh(index=cls.index)

    @classmethod
    def tearDownClass(cls):
        """Remove only the uniquely named index owned by this test."""
        cls.client.indices.delete(index=cls.index)
        cls.client.close()

    def test_counts_are_independent_of_annotation_filter_scores(self):
        """Annotation constraints must not add or subtract affected cases."""
        with patch.object(ssm, "get_es", return_value=self.client), patch.object(
            ssm.settings, "ES_SSM_CENTRIC_INDEX", self.index
        ):
            for cohort, expected in [(["a", "b", "c"], 3), (["a", "b"], 2)]:
                for filters, total in [({}, 2), ({"biotype": ["protein_coding"]}, 1)]:
                    with self.subTest(cohort=cohort, filters=filters):
                        result = ssm.query_ssm_ids(cohort, 10, 0, filters)
                        self.assertEqual(result["ssmsTotal"], total)
                        counts = {v["ssm_id"]: v["score"] for v in result["ssm_ids"]}
                        self.assertEqual(counts["shared"], expected)
                        if not filters:
                            self.assertEqual(counts["rare"], 1)
