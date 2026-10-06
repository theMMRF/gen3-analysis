"""Apply file visibility to every direct Elasticsearch search and count."""

from contextvars import ContextVar
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from elasticsearch import Elasticsearch

from gen3analysis.settings import settings

request_visibility_resources = ContextVar("request_visibility_resources", default=())


def visibility_resources(mapping):
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
                and action.get("service") in ("indexd", "*")
                and action.get("method") in ("read-metadata", "*")
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
                            "script": {
                                "script": {
                                    "source": "def required = doc[params.field]; if (required.size() == 0) return false; for (def resource : required) { if (!params.allowed.containsKey(resource)) return false; } return true;",
                                    "params": {
                                        "field": authz,
                                        "allowed": {
                                            resource: True
                                            for resource in sorted(set(resources))
                                        },
                                    },
                                }
                            }
                        },
                    ]
                }
            }
        )
    return {"bool": {"should": allowed, "minimum_should_match": 1}}


def _unsafe_terms_count(aggregation):
    """Reject term counts that coerce to zero or cannot safely become a long."""
    terms = aggregation.get("terms")
    if not isinstance(terms, dict) or "min_doc_count" not in terms:
        return False
    value = terms["min_doc_count"]
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return True
    try:
        # ES truncates decimal strings before converting to a long; do not round first.
        number = Decimal(str(value))
        return not number.is_finite() or number < 1
    except (InvalidOperation, ValueError, OverflowError):
        return True


def _has_global_aggregation(aggregations):
    return any(
        any(
            key in aggregation
            for key in ("global", "significant_terms", "significant_text")
        )
        or _unsafe_terms_count(aggregation)
        or _has_global_aggregation(aggregation.get("aggs", {}))
        or _has_global_aggregation(aggregation.get("aggregations", {}))
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
    if _has_global_aggregation(body.get("aggs", {})) or _has_global_aggregation(
        body.get("aggregations", {})
    ):
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
