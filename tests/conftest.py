from httpx import AsyncClient, ASGITransport
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import json
from pathlib import Path
from gen3analysis.main import get_app
import gen3analysis.settings

TEST_ACCESS_TOKEN = "123"
TEST_PROJECT_ID = "test-project-id"


@pytest.fixture(scope="session")
def app():
    """Create a test application instance with mocked dependencies"""

    # Load the ES mappings from file
    mappings_file = Path(__file__).parent / "data" / "es_mappings.json"
    with open(mappings_file, "r") as f:
        es_mappings = json.load(f)

    # Mock ES client and registry before creating the app
    with patch("gen3analysis.gen3.es_client.get_es") as mock_get_es, patch(
        "gen3analysis.gen3.es_client.get_nested_registry"
    ) as mock_get_registry:
        # Setup ES mock
        mock_es = MagicMock()
        mock_es.indices.get_mapping.return_value = {"test_index": es_mappings}
        mock_get_es.return_value = mock_es

        # Setup registry mock
        from gen3analysis.settings import settings

        mock_registry = {}
        for index in ["test_index"]:
            mock_reg = Mock()
            mock_reg.index = index
            mock_reg._nested_fields = set()
            mock_reg._reverse_nested_fields = {}
            mock_registry[index] = mock_reg

        mock_get_registry.return_value = mock_registry

        # Create the app
        app = get_app()

        # Mock the guppy client
        mock_guppy = MagicMock()
        app.state.guppy_client = mock_guppy

        yield app


@pytest_asyncio.fixture(scope="function")
async def client(app):
    async with LifespanManager(app):
        # Existing route tests run as an approved metadata reader. Authentication
        # rejection and token propagation are exercised separately in security/.
        with patch.object(
            app.state.arborist_client, "auth_request", AsyncMock(return_value=True)
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": f"Bearer {TEST_ACCESS_TOKEN}"},
            ) as test_client:
                yield test_client
