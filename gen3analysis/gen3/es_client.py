from elasticsearch import Elasticsearch
from gen3analysis.file_visibility import VisibilityElasticsearch
from typing import Optional
from gen3analysis.filters.es.nesting_registry import NestingRegistry
from functools import lru_cache
from gen3analysis.settings import settings, logger

hosts = [h.strip() for h in settings.GEN3_ES_ENDPOINT.split(",") if h.strip()]


@lru_cache
def get_es() -> Elasticsearch:
    logger.info(f"Setting up connection to ES: {hosts}")
    kwargs = {
        "hosts": hosts,
        "use_ssl": settings.ES_VERIFY_SSL,
        "request_timeout": settings.ES_TIMEOUT,
        "timeout": settings.ES_TIMEOUT,
        "retry_on_timeout": True,
    }
    return VisibilityElasticsearch(**kwargs)


@lru_cache
def get_nested_registry() -> dict:
    INDEX_LIST = [
        settings.ES_GENE_CENTRIC_INDEX,
        settings.ES_PROJECT_INDEX,
        settings.ES_SSM_CENTRIC_INDEX,
        settings.ES_SSM_OCCURRENCE_INDEX,
        settings.ES_CNV_CENTRIC_INDEX,
        settings.ES_CNV_OCCURRENCE_INDEX,
        settings.ES_CASE_CENTRIC_INDEX,
        settings.ES_CASE_INDEX,
        settings.ES_FILE_INDEX,
    ]
    es = get_es()
    logger.info(f"Building registry for: {INDEX_LIST} {es.info()}")
    registry = {}
    for index in INDEX_LIST:
        logger.info(f"Building registry for: {index}")
        registry[index] = NestingRegistry.build(es, index)

    return registry


def open_pit(index: str, keep_alive: Optional[str] = None) -> str:
    es = get_es()
    keep_alive = keep_alive or settings.ES_PIT_KEEP_ALIVE
    resp = es.open_point_in_time(index=index, keep_alive=keep_alive)
    return resp["id"]


def close_pit(pit_id: str) -> None:
    es = get_es()
    try:
        es.close_point_in_time(body={"id": pit_id})
    except Exception:
        # If it already expired or closed, ignore.
        pass
