from elasticsearch_dsl import Q
from gen3analysis.filters.es.convert_gql_to_elastic_search import (
    convert_gql_to_elastic_search,
)
from gen3analysis.filters.gen3GQLFilters import (
    GQLFilter,
    GQLEqual,
    GQLNotEqual,
    GQLGreaterThan,
    GQLUnion,
    GQLNestedFilter,
    NestedContents,
)


def test_convert_gql_to_elastic_search_equal():
    gql_filter = GQLEqual(equal_op={"field1": "value1"})
    result = convert_gql_to_elastic_search(gql_filter)
    expected = Q("term", field1="value1")
    assert result == expected


def test_convert_gql_to_elastic_search_not_equal():
    gql_filter = GQLNotEqual(not_equal_op={"field1": "value1"})
    result = convert_gql_to_elastic_search(gql_filter)
    expected = Q("bool", must_not=Q("term", field1="value1"))
    assert result == expected


def test_convert_gql_to_elastic_search_greater_than():
    gql_filter = GQLGreaterThan(greater_than_op={"field1": 10})
    result = convert_gql_to_elastic_search(gql_filter)
    expected = Q("range", field1={"gt": 10})
    assert result == expected


def test_convert_gql_to_elastic_search_union():
    gql_filter = GQLUnion(
        or_op=[
            GQLEqual(equal_op={"field1": "value1"}),
            GQLGreaterThan(greater_than_op={"field2": 10}),
        ]
    )
    result = convert_gql_to_elastic_search(gql_filter)
    expected = Q(
        "bool",
        should=[
            Q("term", field1="value1"),
            Q("range", field2={"gt": 10}),
        ],
    )
    assert result == expected


def test_convert_gql_to_elastic_search_nested():
    gql_filter = GQLNestedFilter(
        nested_op=NestedContents(
            path="nested_field",
            filter_content=GQLEqual(equal_op={"nested_field.inner_field": "value1"}),
        ),
    )
    result = convert_gql_to_elastic_search(gql_filter)
    expected = Q(
        "nested",
        path="nested_field",
        query=Q("term", **{"nested_field.inner_field": "value1"}),
    )
    assert result == expected
