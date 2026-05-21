from typing import Optional, List
from cdislogging import get_logger
from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
import os, sys


def _running_under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


def _select_env_file() -> Optional[str]:
    # gen3analysis/settings.py -> project root is ../
    project_root = Path(__file__).resolve().parent.parent
    project_root_env = project_root / ".env"
    tests_env = project_root / "tests" / ".env"

    if _running_under_pytest() and tests_env.exists():
        return str(tests_env)

    if project_root_env.exists():
        return str(project_root_env)

    return None


def snake_to_pascal(snake_case_string):
    """
    Converts a snake_case string to PascalCase.
    """
    # Replace underscores with spaces, capitalize the first letter of each word,
    # then remove all spaces.
    pascal_case_string = snake_case_string.replace("_", " ").title().replace(" ", "")
    return pascal_case_string


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    GUNICORN_WORKERS: Optional[int] = 7
    GUNICORN_TIMEOUT: int = 210
    GUNICORN_GRACEFUL_TIMEOUT: int = 210
    HOSTNAME: Optional[str] = ""
    DEBUG: Optional[bool] = False

    # Gen3 services
    GUPPY_URL: Optional[str] = "http://guppy-service"
    GUPPY_HTTP_TIMEOUT: int = 180
    ARBORIST_URL: Optional[str] = "http://arborist-service"
    DEPLOYMENT_TYPE: Optional[str] = "prod"

    # Root of the documentation
    URL_PREFIX: Optional[str] = "/analysis/v0"

    # Enabled routes (comma-separated: "basic,compare,survival" or "all")
    ENABLED_ROUTES: Optional[str] = "all"

    # Auth settings
    DEBUG_SKIP_AUTH: Optional[bool] = False
    MOCK_AUTH: Optional[bool] = False

    # Elasticsearch settings
    GEN3_ES_ENDPOINT: Optional[str] = (
        f"http://{os.environ.get('GEN3_ELASTICSEARCH_MASTER_SERVICE_HOST', 'localhost')}:"
        f"{os.environ.get('GEN3_ELASTICSEARCH_MASTER_SERVICE_PORT', '9200')}"
    )
    ES_PIT_KEEP_ALIVE: Optional[str] = "1m"
    ES_ENABLED: Optional[bool] = False

    # Documentation
    DOCS_ROOT: Optional[str] = "/"

    # Top genes configuration
    TOP_GENES_INDEX: Optional[str] = "ssm_occurrence_centric"
    TOP_GENES_GENE_ID_FIELD: Optional[str] = "gene.gene_id"
    TOP_GENES_CASE_NESTED_PATH: Optional[str] = "occurrence.case"
    TOP_GENES_CASE_ID_FIELD: Optional[str] = "occurrence.case.case_id"
    TOP_GENES_PROJECT_FIELD: Optional[str] = "occurrence.case.project.project_id"

    # GraphQL settings
    GRAPHQL_ENABLED: Optional[bool] = True

    # Case ID cache
    CASE_ID_CACHE_MAX_SIZE: int = 128
    CASE_ID_CACHE_TTL: int = 300  # seconds; used by Redis cache
    REDIS_URL: Optional[str] = (
        None  # e.g. "redis://localhost:6379"; disables in-process cache when set
    )

    # Terms acceptance PostgreSQL database
    TERMS_ACCEPTANCE_DATABASE_URL: Optional[str] = None


class GDCGenomicSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    ES_VERIFY_SSL: Optional[bool] = False
    ES_CA_CERT: Optional[str] = None
    ES_TIMEOUT: int = 180

    MAX_CASES: Optional[int] = 10000
    # Guppy default indices
    CASE_INDEX: Optional[str] = "case"
    FILE_INDEX: Optional[str] = "file"
    PROJECT_INDEX: Optional[str] = "project"
    GENE_CENTRIC_INDEX: Optional[str] = "gene_centric"
    SSM_CENTRIC_INDEX: Optional[str] = "ssm_centric"
    SSM_OCCURRENCE_CENTRIC_INDEX: Optional[str] = "ssm_occurrence_centric"
    CNV_CENTRIC_INDEX: Optional[str] = "cnv_centric"
    CNV_OCCURRENCE_CENTRIC_INDEX: Optional[str] = "cnv_occurrence_centric"
    CASE_CENTRIC_INDEX: Optional[str] = "case_centric"
    CASE_CENTRIC_AGGREGATION_INDEX: Optional[str] = "case_centric"

    # Elastic search indices for MMRF and GDC
    ES_CASE_CENTRIC_INDEX: Optional[str] = "case_centric"
    ES_CASE_INDEX: Optional[str] = "case"
    ES_FILE_INDEX: Optional[str] = "file"
    ES_PROJECT_INDEX: Optional[str] = "project"
    ES_GENE_CENTRIC_INDEX: Optional[str] = "gene_centric"
    ES_SSM_CENTRIC_INDEX: Optional[str] = "ssm_centric"
    ES_SSM_OCCURRENCE_INDEX: Optional[str] = "ssm_occurrence_centric"
    ES_CNV_CENTRIC_INDEX: Optional[str] = "cnv_centric"
    ES_CNV_OCCURRENCE_INDEX: Optional[str] = "cnv_occurrence_centric"

    @classmethod
    def compute_gql_index(cls, index: str) -> str:
        return f"{snake_to_pascal(index)}_{index}"

    @classmethod
    def compute_gql_agg_index(cls, index: str) -> str:
        return f"{snake_to_pascal(index)}__aggregation"

    @computed_field
    @property
    def case_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_index(self.CASE_INDEX)

    @computed_field
    @property
    def case_agg_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_agg_index(self.CASE_INDEX)

    @computed_field
    @property
    def file_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_index(self.FILE_INDEX)

    @computed_field
    @property
    def file_agg_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_agg_index(self.FILE_INDEX)

    @computed_field
    @property
    def project_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_index(self.PROJECT_INDEX)

    @computed_field
    @property
    def project_agg_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_agg_index(self.PROJECT_INDEX)

    @computed_field
    @property
    def gene_centric_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_index(self.GENE_CENTRIC_INDEX)

    @computed_field
    @property
    def gene_centric_agg_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_agg_index(self.GENE_CENTRIC_INDEX)

    @computed_field
    @property
    def ssm_centric_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_index(self.SSM_CENTRIC_INDEX)

    @computed_field
    @property
    def ssm_centric_agg_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_agg_index(self.SSM_CENTRIC_INDEX)

    @computed_field
    @property
    def ssm_occurrence_centric_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_index(self.SSM_OCCURRENCE_CENTRIC_INDEX)

    @computed_field
    @property
    def ssm_occurrence_centric_agg_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_agg_index(
            self.SSM_OCCURRENCE_CENTRIC_INDEX
        )

    @computed_field
    @property
    def cnv_centric_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_index(self.CNV_CENTRIC_INDEX)

    @computed_field
    @property
    def cnv_centric_agg_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_agg_index(self.CNV_CENTRIC_INDEX)

    @computed_field
    @property
    def cnv_occurrence_centric_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_index(self.CNV_OCCURRENCE_CENTRIC_INDEX)

    @computed_field
    @property
    def cnv_occurrence_centric_agg_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_agg_index(
            self.CNV_OCCURRENCE_CENTRIC_INDEX
        )

    @computed_field
    @property
    def case_centric_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_index(self.CASE_CENTRIC_INDEX)

    @computed_field
    @property
    def case_centric_agg_gql(self) -> str:
        return GDCGenomicSettings.compute_gql_agg_index(self.CASE_CENTRIC_INDEX)


class GeneExpressionSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    GENE_EXPRESSION_SQLITE_PATH: Optional[str] = (
        "data/mmrf_gene_expression_test_data/schemaless.sqlite3"
    )
    GENE_EXPRESSION_DATA_DIR: Optional[str] = (
        "data/mmrf_gene_expression_test_data/mmrf_test_data"
    )
    GENE_EXPRESSION_ENABLED: bool = True


SettingsRegistry = {
    "core": CoreSettings,
    "cohort": GDCGenomicSettings,
    "gene_expression": GeneExpressionSettings,
}


def create_settings(enabled_routes: List[str]) -> BaseSettings:
    """
    Create settings by composing multiple settings classes based on enabled routes.

    Args:
        enabled_routes: List of route names to enable (e.g., ['core', 'guppy', 'cohortCentric'])
    """
    bases = tuple(SettingsRegistry[r] for r in enabled_routes if r in SettingsRegistry)
    CombinedSettings = type(
        "CombinedSettings", bases, {"model_config": SettingsConfigDict(extra="ignore")}
    )
    return CombinedSettings(_env_file=_select_env_file(), _env_file_encoding="utf-8")


# Determine enabled routes from environment variable
# Format: comma-separated list like "basic,compare,survival,cohorts"
# If not set, all routes are enabled by default
ENABLED_ROUTES_ENV = os.environ.get("ENABLED_ROUTES", "all")
if ENABLED_ROUTES_ENV == "all":
    ENABLED_ROUTES = [
        "core",
        "compare",
        "survival",
        "cohort",
        "gene_expression",
        "genomic",
        "cases",
        "files",
        "ssms",
        "ssm_occurrence",
        "cnv",
        "cnv_occurrence",
        "terms",
    ]
else:
    # Always include core, add user-specified routes
    ENABLED_ROUTES = ["core"] + [
        r.strip() for r in ENABLED_ROUTES_ENV.split(",") if r.strip()
    ]

settings = create_settings(ENABLED_ROUTES)

logger = get_logger("gen3-analysis", log_level="debug" if settings.DEBUG else "debug")
