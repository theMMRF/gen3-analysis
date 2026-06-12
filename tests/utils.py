from unittest.mock import MagicMock, Mock, AsyncMock

from gen3analysis.filters.es.nesting_registry import NestingRegistry


def mock_guppy_data(app, data_list):
    """
    data_list: A list of data objects to return on successive calls
    """
    mocked_guppy_client = MagicMock()

    # AsyncMock is specifically designed for async functions
    mocked_execute_function = AsyncMock(side_effect=data_list)
    mocked_guppy_client.execute = mocked_execute_function
    mocked_guppy_client.close = AsyncMock()
    app.state.guppy_client = mocked_guppy_client


def mock_es_client(app):
    """
    Mock the Elasticsearch client to prevent actual ES connections during tests.
    """

    # Create a mock ES client
    mock_es = MagicMock()

    # Mock the indices.get_mapping method
    mock_es.indices.get_mapping.return_value = {}

    # Create a mock NestingRegistry
    mock_registry = Mock(spec=NestingRegistry)
    mock_registry.index = app.state.settings.ES_CASE_CENTRIC_INDEX
    mock_registry._nested_fields = set()
    mock_registry._reverse_nested_fields = {}

    # Patch the get_es and get_nested_registry functions
    import gen3analysis.gen3.es_client as es_client_module

    es_client_module._es_client = mock_es
    es_client_module._nested_registry = {
        app.state.settings.ES_CASE_CENTRIC_INDEX: mock_registry
    }

    return mock_es
