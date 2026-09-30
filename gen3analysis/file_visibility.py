"""Apply file visibility to every direct Elasticsearch search and count."""

from contextvars import ContextVar
from copy import deepcopy

from elasticsearch import Elasticsearch

from gen3analysis.settings import settings

request_visibility_resources = ContextVar("request_visibility_resources", default=())


def downloadable_resources(mapping):
    if not isinstance(mapping, dict) or any(
        not isinstance(actions, list) for actions in mapping.values()
    ):
        raise ValueError("Invalid authorization mapping")
    return tuple(
        sorted(
            resource
            for resource, actions in mapping.items()
            if any(
                isinstance(action, dict)
                and action.get("service") in ("fence", "*")
                and action.get("method") in ("read-storage", "*")
                for action in actions
            )
        )
    )


def visibility_query(resources):
    visibility = "_gen3_visibility"
    authz = "_gen3_visibility_authz"
    allowed = [
        {"term": {visibility: "public"}},
    ]
    if resources:
        allowed.append(
            {
                "bool": {
                    "filter": [
                        {"term": {visibility: "restricted"}},
                        {"exists": {"field": authz}},
                        {
                            "terms_set": {
                                authz: {
                                    "terms": sorted(set(resources)),
                                    "minimum_should_match_script": {
                                        "source": "doc[params.field].size()",
                                        "params": {"field": authz},
                                    },
                                }
                            }
                        },
                    ]
                }
            }
        )
    return {"bool": {"should": allowed, "minimum_should_match": 1}}


def _has_global_aggregation(aggregations):
    return any(
        any(
            key in aggregation
            for key in ("global", "significant_terms", "significant_text")
        )
        or _has_global_aggregation(
            aggregation.get("aggs", aggregation.get("aggregations", {}))
        )
        for aggregation in aggregations.values()
    )


def apply_visibility(body):
    if not settings.FILE_VISIBILITY_ENABLED:
        return body
    body = deepcopy(body or {})
    if body.get("suggest") or {
        "_gen3_visibility",
        "_gen3_visibility_authz",
    }.intersection(body.get("runtime_mappings", {})):
        raise ValueError("Query cannot override or bypass file visibility")
    if _has_global_aggregation(body.get("aggs", body.get("aggregations", {}))):
        raise ValueError("Aggregations cannot bypass file visibility")
    body["query"] = {
        "bool": {
            "filter": [
                body.get("query", {"match_all": {}}),
                visibility_query(request_visibility_resources.get()),
            ]
        }
    }
    return body


class VisibilityElasticsearch(Elasticsearch):
    """Policy filtering sits below every DSL query builder, including PIT queries."""

    def search(self, body=None, index=None, params=None, headers=None, **kwargs):
        return super().search(
            body=apply_visibility(body),
            index=index,
            params=params,
            headers=headers,
            **kwargs
        )

    def count(self, body=None, index=None, params=None, headers=None, **kwargs):
        return super().count(
            body=apply_visibility(body),
            index=index,
            params=params,
            headers=headers,
            **kwargs
        )
