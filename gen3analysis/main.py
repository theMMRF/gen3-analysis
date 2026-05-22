import asyncio
from contextlib import asynccontextmanager
from importlib.metadata import version

import fastapi
from cdislogging import get_logger
from fastapi import FastAPI, APIRouter
from fastapi.exceptions import RequestValidationError
from gen3authz.client.arborist.async_client import ArboristClient
from starlette.requests import Request
from starlette.responses import JSONResponse

from gen3analysis.auth import Gen3SdkAuth
from gen3analysis.gen3.es_client import get_nested_registry
from gen3analysis.gen3.guppyQuery import GuppyGQLClient
from gen3analysis.settings import settings, logger
from gen3analysis.terms_acceptance.database import (
    create_terms_acceptance_engine,
    create_terms_acceptance_sessionmaker,
    get_terms_database_config,
)

route_aggregator = APIRouter()

# Map route names to their module paths (import_path, router_name, prefix, tags)
ALL_ROUTE_DEFINITIONS = {
    "basic": ("gen3analysis.routes.basic", "basic_router", "", ["Basic"]),
    "compare": ("gen3analysis.routes.compare", "compare", "/compare", ["Compare"]),
    "survival": ("gen3analysis.routes.survival", "survival", "/survival", ["Survival"]),
    "cohorts": ("gen3analysis.routes.cohorts", "cohorts", "/cohorts", ["Cohorts"]),
    "genomic": ("gen3analysis.routes.genomic", "genomic", "/genomic", ["Genomic"]),
    "cases": ("gen3analysis.routes.cases", "cases", "/cases", ["Cases"]),
    "files": ("gen3analysis.routes.files", "files", "/files", ["Files"]),
    "ssms": ("gen3analysis.routes.ssms", "ssms", "/ssms", ["SSMS"]),
    "ssms_occurrence": (
        "gen3analysis.routes.ssm_occurrence",
        "ssms_occurrence",
        "/ssms_occurrence",
        ["SSMS Occurrence"],
    ),
    "gene_expression": (
        "gen3analysis.routes.gene_expression",
        "gene_expression",
        "/gene_expression",
        ["Gene Expression"],
    ),
    "cnv": ("gen3analysis.routes.cnv", "cnv", "/cnv", ["CNV"]),
    "cnv_occurrence": (
        "gen3analysis.routes.cnv_occurrence",
        "cnv_occurrence",
        "/cnv_occurrence",
        ["CNV Occurrence"],
    ),
    "terms": ("gen3analysis.routes.terms", "terms", "/terms", ["Terms"]),
}

# Get enabled routes from environment variable
# Format: comma-separated list like "basic,compare,survival"
# If not set or "all", enable all routes
enabled_routes_env = (
    settings.ENABLED_ROUTES if hasattr(settings, "ENABLED_ROUTES") else None
)
if not enabled_routes_env:
    import os

    enabled_routes_env = os.environ.get("ENABLED_ROUTES", "all")

if enabled_routes_env == "all":
    enabled_routes = list(ALL_ROUTE_DEFINITIONS.keys())
else:
    enabled_routes = [r.strip() for r in enabled_routes_env.split(",") if r.strip()]

# Include only enabled routes - import dynamically to avoid importing disabled routes
for route_name in enabled_routes:
    if route_name in ALL_ROUTE_DEFINITIONS:
        import importlib

        module_path, router_name, prefix, tags = ALL_ROUTE_DEFINITIONS[route_name]
        module = importlib.import_module(module_path)
        router = getattr(module, router_name)
        route_aggregator.include_router(router, prefix=prefix, tags=tags)
        logger.info(f"Enabled route: {route_name}")
    else:
        logger.warning(f"Unknown route requested: {route_name}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Parse the configuration, setup, and instantiate necessary classes.

    This is FastAPI's way of dealing with startup logic before the app
    starts receiving requests.

    https://fastapi.tiangolo.com/advanced/events/#lifespan

    Args:
        app (fastapi.FastAPI): The FastAPI app object
    """
    # startup
    if settings.DEPLOYMENT_TYPE == "prod":
        guppy_url = "http://guppy-service"
        revproxy_url = "http://revproxy-service"
    else:
        guppy_url = f"{settings.GUPPY_URL}"
        revproxy_url = f"{settings.HOSTNAME}"

    guppy_client = GuppyGQLClient(
        graphql_url=f"{guppy_url}/graphql", csrf_token_url=revproxy_url
    )
    if settings.ES_ENABLED:
        get_nested_registry()

    app.state.guppy_client = guppy_client
    app.state.gen3_sdk_auth = None
    if settings.DEPLOYMENT_TYPE == "dev":
        app.state.gen3_sdk_auth = Gen3SdkAuth(endpoint=settings.HOSTNAME)

    app.state.arborist_client = ArboristClient(
        arborist_base_url=settings.ARBORIST_URL,
        logger=get_logger(
            "gen3analysis.gen3authz", log_level="debug" if settings.DEBUG else "info"
        ),
    )
    if "terms" in enabled_routes:
        app.state.terms_acceptance_engine = create_terms_acceptance_engine(
            get_terms_database_config(settings)
        )
        app.state.terms_acceptance_sessionmaker = create_terms_acceptance_sessionmaker(
            app.state.terms_acceptance_engine
        )
    else:
        app.state.terms_acceptance_engine = None
        app.state.terms_acceptance_sessionmaker = None

    # Initialize gene expression data store
    if settings.ENABLED_ROUTES and "gene_expression" in settings.ENABLED_ROUTES:
        from gen3analysis.gene_expression.data_store import GeneExpressionDataStore

        try:
            if (
                settings.GENE_EXPRESSION_SQLITE_PATH
                and settings.GENE_EXPRESSION_DATA_DIR
            ):
                logger.info(
                    "Initializing gene expression data store from %s",
                    settings.GENE_EXPRESSION_DATA_DIR,
                )
                GeneExpressionDataStore.get_instance(
                    sqlite_path=settings.GENE_EXPRESSION_SQLITE_PATH,
                    data_dir=settings.GENE_EXPRESSION_DATA_DIR,
                )
                logger.info("Gene expression data store initialized successfully")
            else:
                logger.warning(
                    "Gene expression data paths not configured and fallback disabled"
                )
        except Exception as e:
            logger.error("Failed to initialize gene expression data store: %s", e)
            # Don't fail the app startup if gene expression data store initialization fails'

    yield

    # teardown
    if app.state.guppy_client:
        await app.state.guppy_client.close()
    app.state.guppy_client = None
    app.state.gdc_graphql_client = None
    app.state.gen3_sdk_auth = None
    app.state.arborist_client = None
    if app.state.terms_acceptance_engine:
        await app.state.terms_acceptance_engine.dispose()
    app.state.terms_acceptance_engine = None
    app.state.terms_acceptance_sessionmaker = None

    # Reset gene expression data store
    if settings.GENE_EXPRESSION_ENABLED:
        from gen3analysis.gene_expression.data_store import GeneExpressionDataStore

        GeneExpressionDataStore.reset_instance()

    # NOTE: multiprocess.mark_process_dead is called by the gunicorn "child_exit" function for each
    # worker. "child_exit" is defined in the gunicorn.conf.py


def get_app() -> fastapi.FastAPI:
    """
    Return the web framework app object after adding routes

    Returns:
        fastapi.FastAPI: The FastAPI app object
    """

    fastapi_app = FastAPI(
        title="Gen3 Analysis Service",
        version=version("gen3analysis"),
        debug=settings.DEBUG,
        root_path=settings.URL_PREFIX,
        lifespan=lifespan,
    )
    fastapi_app.include_router(route_aggregator)
    fastapi_app.add_middleware(ClientDisconnectMiddleware)

    return fastapi_app


class ClientDisconnectMiddleware:
    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        loop = asyncio.get_running_loop()
        rv = loop.create_task(self._app(scope, receive, send))
        waiter = None
        cancelled = False
        if scope["type"] == "http":

            def add_close_watcher():
                nonlocal waiter

                async def wait_closed():
                    nonlocal cancelled
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            if not rv.done():
                                cancelled = True
                                rv.cancel()
                            break

                waiter = loop.create_task(wait_closed())

            scope["add_close_watcher"] = add_close_watcher
        try:
            await rv
        except asyncio.CancelledError:
            if not cancelled:
                raise
        if waiter and not waiter.done():
            waiter.cancel()


app_instance = get_app()


@app_instance.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = []
    for err in exc.errors():
        # err["loc"] like ("body", "filter", 0, "name")
        path = ".".join(str(p) for p in err["loc"])
        errors.append({"path": path, "msg": err["msg"], "type": err["type"]})
    return JSONResponse(status_code=422, content={"detail": errors})
