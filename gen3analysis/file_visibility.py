"""Apply file visibility to every direct Elasticsearch search and count."""

from contextvars import ContextVar
import base64
import hashlib
import hmac
import json
import re
import time
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
        any(key in aggregation for key in ("significant_terms", "significant_text"))
        or _unsafe_terms_count(aggregation)
        or _has_global_aggregation(aggregation.get("aggs", {}))
        or _has_global_aggregation(aggregation.get("aggregations", {}))
        for aggregation in aggregations.values()
    )


FILE_AUTHZ = "_gen3_file_authz"
FILE_VERSION = "_gen3_file_visibility_version"
VISIBLE_AGG = "__gen3_visible_files"
SUMMARY_FIELDS = (
    "file_count",
    "file_size",
    "data_categories",
    "experimental_strategies",
)


def ownership_query(resources, field=FILE_AUTHZ):
    return {
        "bool": {
            "filter": [
                {"exists": {"field": field}},
                {
                    "script": {
                        "script": {
                            "source": "def required = doc[params.field]; if (required.size() == 0) return false; for (def resource : required) { if (!params.allowed.containsKey(resource)) return false; } return true;",
                            "params": {
                                "field": field,
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


def visibility_query(resources):
    return {
        "bool": {
            "filter": [
                {"term": {FILE_VERSION: 1}},
                {
                    "bool": {
                        "should": [
                            {
                                "bool": {
                                    "must_not": [
                                        {"exists": {"field": field}}
                                        for field in (
                                            FILE_AUTHZ,
                                            "file_id",
                                            "object_id",
                                            "did",
                                            "file_name",
                                        )
                                    ]
                                }
                            },
                            ownership_query(resources),
                        ],
                        "minimum_should_match": 1,
                    }
                },
            ]
        }
    }


def protected_file_paths(properties):
    if FILE_VERSION not in properties or FILE_AUTHZ not in properties:
        raise ValueError(
            "Search projection has not been prepared for project visibility"
        )
    fields, paths = {}, {}

    def visit(props, prefix=""):
        for name, definition in props.items():
            path = prefix + name
            fields[path] = definition
            if "properties" in definition:
                visit(definition["properties"], path + ".")

    visit(properties)
    for field, definition in fields.items():
        if field == FILE_AUTHZ or field.endswith("." + FILE_AUTHZ):
            if (
                definition.get("type") != "keyword"
                or definition.get("doc_values") is False
            ):
                raise ValueError("File ownership requires keyword doc values")
            if field != FILE_AUTHZ:
                path = field[: -len(FILE_AUTHZ) - 1]
                if (
                    fields[path].get("type") != "nested"
                    or fields[path].get("include_in_parent")
                    or fields[path].get("include_in_root")
                ):
                    raise ValueError("File ownership containers must be nested")
                paths[path] = field
    for field, definition in fields.items():
        owners = [path for path in paths if field.startswith(path + ".")]
        if owners and definition.get("copy_to"):
            owner = max(owners, key=len)
            targets = definition["copy_to"]
            targets = targets if isinstance(targets, list) else [targets]
            if any(not target.startswith(owner + ".") for target in targets):
                raise ValueError("File fields cannot copy into an unfiltered parent")
    return paths


_SUMMARY_FIELD = re.compile(
    r"(?:^|\.)summary\.(?:file_count|file_size|data_categories|experimental_strategies)(?:\.|$)"
)
_FORBIDDEN = {
    "script",
    "script_fields",
    "runtime_mappings",
    "suggest",
    "highlight",
    "fields",
    "docvalue_fields",
    "stored_fields",
    "query_string",
    "simple_query_string",
    "buckets_path",
    "has_child",
    "has_parent",
    "parent_id",
    "profile",
    "explain",
}


def _check_query(value, parent=""):
    if isinstance(value, list):
        for item in value:
            _check_query(item, parent)
    elif isinstance(value, dict):
        if "aggs" in value and "aggregations" in value:
            raise ValueError("Use one aggregation alias")
        for name, item in value.items():
            if name in ("_source", "includes", "excludes"):
                continue
            if name in _FORBIDDEN or name == VISIBLE_AGG:
                raise ValueError("Query cannot bypass project visibility")
            if (
                _SUMMARY_FIELD.search(name)
                or (name == "field" or parent == "sort")
                and isinstance(item, str)
                and _SUMMARY_FIELD.search(item)
            ):
                raise ValueError("Query file summaries through the filtered file index")
            if (
                name == "order"
                and isinstance(item, (dict, list))
                and any(
                    not isinstance(order, dict)
                    or any(field not in ("_key", "_count") for field in order)
                    for order in (item if isinstance(item, list) else [item])
                )
            ):
                raise ValueError(
                    "Custom aggregation ordering can expose unfiltered file counts"
                )
            if (
                name == "terms"
                and isinstance(item, dict)
                and any(
                    isinstance(option, dict) and "index" in option
                    for option in item.values()
                )
            ):
                raise ValueError("Terms lookup cannot bypass project visibility")
            _check_query(item, name)


def _filtered(query, guard):
    return {"bool": {"filter": [query or {"match_all": {}}, guard]}}


def _rewrite_query(value, paths, resources):
    if isinstance(value, list):
        return [_rewrite_query(item, paths, resources) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: _rewrite_query(item, paths, resources) for key, item in value.items()
    }
    nested = result.get("nested")
    if isinstance(nested, dict) and "query" in nested and nested.get("path") in paths:
        nested["query"] = _filtered(
            nested["query"], ownership_query(resources, paths[nested["path"]])
        )
    return result


def _rewrite_aggs(aggregations, paths, resources):
    result = {}
    for name, original in (aggregations or {}).items():
        agg = _rewrite_query(original, paths, resources)
        children_key = "aggregations" if "aggregations" in agg else "aggs"
        children = _rewrite_aggs(agg.get(children_key, {}), paths, resources)
        if children:
            agg[children_key] = children
        path = agg.get("nested", {}).get("path")
        if path in paths:
            agg[children_key] = {
                VISIBLE_AGG: {
                    "filter": ownership_query(resources, paths[path]),
                    "aggs": children,
                }
            }
        if "global" in agg:
            agg[children_key] = {
                VISIBLE_AGG: {"filter": visibility_query(resources), "aggs": children}
            }
        if "top_hits" in agg:
            agg["top_hits"]["_source"] = True
            if "sort" in agg["top_hits"]:
                agg["top_hits"]["sort"] = _rewrite_sort(
                    agg["top_hits"]["sort"], paths, resources
                )
        result[name] = agg
    return result


def _rewrite_sort(sort, paths, resources):
    result = []
    for entry in sort if isinstance(sort, list) else [sort]:
        if not isinstance(entry, dict):
            if isinstance(entry, str) and any(
                entry.startswith(path + ".") for path in paths
            ):
                raise ValueError("File sorting requires a correlated nested sort")
            result.append(entry)
            continue
        changed = {}
        for field, options in entry.items():
            owners = [path for path in paths if field.startswith(path + ".")]
            if not owners:
                changed[field] = options
                continue
            owner = max(owners, key=len)
            options = (
                deepcopy(options) if isinstance(options, dict) else {"order": options}
            )
            if options.get("nested", {}).get("path") != owner:
                raise ValueError("File sorting requires a correlated nested sort")
            options["nested"]["filter"] = _filtered(
                _rewrite_query(options["nested"].get("filter"), paths, resources),
                ownership_query(resources, paths[owner]),
            )
            changed[field] = options
        result.append(changed)
    return result


def apply_visibility(body, paths=None):
    if not settings.PROJECT_VISIBILITY_ENABLED:
        return body
    if paths is None:
        raise ValueError("Search ownership mapping is required")
    body = deepcopy(body or {})
    _check_query(body)
    if _has_global_aggregation(body.get("aggs", {})) or _has_global_aggregation(
        body.get("aggregations", {})
    ):
        raise ValueError("Aggregations cannot bypass file visibility")
    resources = request_visibility_resources.get()
    body["query"] = _filtered(
        _rewrite_query(body.get("query"), paths, resources), visibility_query(resources)
    )
    body["_source"] = True
    if "post_filter" in body:
        body["post_filter"] = _rewrite_query(body["post_filter"], paths, resources)
    for key in ("aggs", "aggregations"):
        if key in body:
            body[key] = _rewrite_aggs(body[key], paths, resources)
    if "sort" in body:
        body["sort"] = _rewrite_sort(body["sort"], paths, resources)
    return body


def _readable(required, resources):
    return (
        isinstance(required, list)
        and bool(required)
        and all(
            isinstance(resource, str) and resource in resources for resource in required
        )
    )


def redact_file_source(source, resources):
    if isinstance(source, list):
        return [
            clean
            for clean in (redact_file_source(item, resources) for item in source)
            if clean is not None
        ]
    if not isinstance(source, dict):
        return source
    if FILE_AUTHZ in source and not _readable(source[FILE_AUTHZ], resources):
        return None
    result = {}
    for key, value in source.items():
        if key.startswith("_gen3_file_"):
            continue
        clean = redact_file_source(value, resources)
        if clean is not None:
            result[key] = clean
    if isinstance(source.get("summary"), dict):
        summary = result.setdefault("summary", {})
        for key in SUMMARY_FIELDS:
            summary.pop(key, None)
        if isinstance(source.get("_gen3_file_summary"), list):
            rows = [
                row
                for row in source["_gen3_file_summary"]
                if _readable(row.get("authz"), resources)
            ]
            summary["file_count"] = sum(row["file_count"] for row in rows)
            summary["file_size"] = sum(row["file_size"] for row in rows)
            for field, plural in (
                ("data_category", "data_categories"),
                ("experimental_strategy", "experimental_strategies"),
            ):
                groups = {}
                for row in rows:
                    values = row.get(field, [])
                    values = values if isinstance(values, list) else [values]
                    for value in values:
                        group = groups.setdefault(value, {"count": 0, "cases": set()})
                        group["count"] += row["file_count"]
                        group["cases"].update(row.get("case_ids", []))
                summary[plural] = [
                    {
                        field: value,
                        "file_count": group["count"],
                        **(
                            {"case_count": len(group["cases"])}
                            if "case_count" in source["summary"]
                            else {}
                        ),
                    }
                    for value, group in sorted(groups.items())
                ]
    return result


def redact_file_response(value, resources):
    if isinstance(value, list):
        return [redact_file_response(item, resources) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: (
            (redact_file_source(item, resources) or {})
            if key == "_source"
            else redact_file_response(item, resources)
        )
        for key, item in value.items()
        if key != VISIBLE_AGG
    }
    if VISIBLE_AGG in value:
        result.update(redact_file_response(value[VISIBLE_AGG], resources))
    return result


def project_file_source(source, selection):
    if selection is None or selection is True:
        return source
    if selection is False:
        return {}
    filters = (
        selection
        if isinstance(selection, dict)
        else {"includes": selection if isinstance(selection, list) else [selection]}
    )
    includes = filters.get("includes", filters.get("include", ["*"]))
    excludes = filters.get("excludes", filters.get("exclude", []))
    includes = includes if isinstance(includes, list) else [includes]
    excludes = excludes if isinstance(excludes, list) else [excludes]

    def matches(pattern, path):
        return bool(
            re.match(
                "^"
                + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
                + r"(?:\.|$)",
                path,
            )
        )

    def visit(value, path="", inherited=False):
        if path and any(matches(pattern, path) for pattern in excludes):
            return None
        selected = inherited or bool(
            path and any(matches(pattern, path) for pattern in includes)
        )
        if isinstance(value, list):
            items = [
                clean
                for clean in (visit(item, path, selected) for item in value)
                if clean is not None
            ]
            return (
                items
                if items
                or selected
                or any(pattern.startswith(path + ".") for pattern in includes)
                else None
            )
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                clean = visit(item, (path + "." if path else "") + key, selected)
                if clean is not None:
                    result[key] = clean
            return result or None
        return value if selected else None

    return visit(source) or {}


class VisibilityElasticsearch(Elasticsearch):
    """Apply ownership before queries/counts, then redact and project responses."""

    def _layout(self, index):
        if not index:
            raise ValueError("An explicit served index or bound PIT is required")
        mappings = self.indices.get_mapping(index=index)
        layouts = [
            value["mappings"].get("properties", {}) for value in mappings.values()
        ]
        if not layouts or any(layout != layouts[0] for layout in layouts[1:]):
            raise ValueError("Search alias has inconsistent ownership mappings")
        return list(mappings), protected_file_paths(layouts[0])

    @staticmethod
    def _safe_parameters(params, kwargs):
        overrides = {
            "q",
            "scroll",
            "source",
            "filter_path",
            "_source",
            "_source_includes",
            "_source_excludes",
            "stored_fields",
            "docvalue_fields",
            "fields",
        }
        if overrides.intersection(params or {}) or overrides.intersection(kwargs):
            raise ValueError("Query parameters cannot bypass project visibility")

    @staticmethod
    def _pit_key():
        key = settings.PROJECT_VISIBILITY_CURSOR_KEY
        if not key or len(key.encode("utf-8")) < 32:
            raise ValueError(
                "PROJECT_VISIBILITY_CURSOR_KEY must contain at least 32 bytes"
            )
        return key.encode("utf-8")

    def _seal_pit(self, identifier, physical, keep_alive):
        duration = re.fullmatch(r"([0-9]+)(ms|s|m|h|d)", keep_alive or "1m")
        if not duration:
            raise ValueError("Invalid PIT lifetime")
        seconds = (
            int(duration[1])
            * {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}[duration[2]]
        )
        payload = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "pit": identifier,
                    "indices": physical,
                    "expires": time.time() + min(seconds, 3600),
                },
                separators=(",", ":"),
            ).encode()
        ).decode()
        signature = hmac.new(
            self._pit_key(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return payload + "." + signature

    def _unseal_pit(self, token):
        try:
            if not isinstance(token, str) or len(token) > 262144:
                raise ValueError()
            payload, signature = token.rsplit(".", 1)
            expected = hmac.new(
                self._pit_key(), payload.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            binding = json.loads(base64.urlsafe_b64decode(payload))
            if (
                binding["expires"] <= time.time()
                or not binding["indices"]
                or not binding["pit"]
            ):
                raise ValueError()
            return binding
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("PIT is unbound or expired; restart pagination") from exc

    def open_point_in_time(self, index, params=None, headers=None, **kwargs):
        if not settings.PROJECT_VISIBILITY_ENABLED:
            return super().open_point_in_time(
                index=index, params=params, headers=headers, **kwargs
            )
        self._pit_key()
        physical, _ = self._layout(index)
        response = super().open_point_in_time(
            index=physical, params=params, headers=headers, **kwargs
        )
        response["id"] = self._seal_pit(
            response["id"],
            physical,
            kwargs.get("keep_alive") or (params or {}).get("keep_alive", "1m"),
        )
        return response

    def close_point_in_time(self, body=None, params=None, headers=None, **kwargs):
        if settings.PROJECT_VISIBILITY_ENABLED:
            if not isinstance(body, dict):
                raise ValueError("A bound PIT is required")
            body = {**body, "id": self._unseal_pit(body.get("id"))["pit"]}
        return super().close_point_in_time(
            body=body, params=params, headers=headers, **kwargs
        )

    def search(self, body=None, index=None, params=None, headers=None, **kwargs):
        if not settings.PROJECT_VISIBILITY_ENABLED:
            return super().search(
                body=body, index=index, params=params, headers=headers, **kwargs
            )
        self._safe_parameters(params, kwargs)
        selection = (body or {}).get("_source")
        pit = (body or {}).get("pit")
        if pit:
            if index:
                raise ValueError("PIT searches cannot override the bound index")
            binding = self._unseal_pit(pit.get("id"))
            bound_physical, paths = self._layout(binding["indices"])
            if set(bound_physical) != set(binding["indices"]):
                raise ValueError("PIT physical index binding changed")
            body = deepcopy(body)
            body["pit"]["id"] = binding["pit"]
            physical = None
        else:
            physical, paths = self._layout(index)
        response = super().search(
            body=apply_visibility(body, paths),
            index=physical,
            params=params,
            headers=headers,
            **kwargs
        )
        if pit and response.get("pit_id"):
            response["pit_id"] = self._seal_pit(
                response["pit_id"], bound_physical, pit.get("keep_alive", "1m")
            )
        response = redact_file_response(response, request_visibility_resources.get())
        for hit in response.get("hits", {}).get("hits", []):
            if "_source" in hit:
                hit["_source"] = project_file_source(hit["_source"], selection)
        return response

    def count(self, body=None, index=None, params=None, headers=None, **kwargs):
        if not settings.PROJECT_VISIBILITY_ENABLED:
            return super().count(
                body=body, index=index, params=params, headers=headers, **kwargs
            )
        self._safe_parameters(params, kwargs)
        physical, paths = self._layout(index)
        body = apply_visibility(body, paths)
        body.pop("_source", None)
        return super().count(
            body=body, index=physical, params=params, headers=headers, **kwargs
        )

    def _unsupported_read(self, method, *args, **kwargs):
        if settings.PROJECT_VISIBILITY_ENABLED:
            raise ValueError(
                "Use filtered search/count; this read API is disabled with project visibility"
            )
        return getattr(super(), method)(*args, **kwargs)

    def scroll(self, *args, **kwargs):
        return self._unsupported_read("scroll", *args, **kwargs)

    def msearch(self, *args, **kwargs):
        return self._unsupported_read("msearch", *args, **kwargs)

    def get(self, *args, **kwargs):
        return self._unsupported_read("get", *args, **kwargs)

    def mget(self, *args, **kwargs):
        return self._unsupported_read("mget", *args, **kwargs)

    def get_source(self, *args, **kwargs):
        return self._unsupported_read("get_source", *args, **kwargs)

    def explain(self, *args, **kwargs):
        return self._unsupported_read("explain", *args, **kwargs)

    def termvectors(self, *args, **kwargs):
        return self._unsupported_read("termvectors", *args, **kwargs)

    def mtermvectors(self, *args, **kwargs):
        return self._unsupported_read("mtermvectors", *args, **kwargs)
