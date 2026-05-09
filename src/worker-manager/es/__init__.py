"""Elasticsearch client package for the Flume worker-manager.

Provides a unified, connection-pooled ES client that replaces the
duplicate urllib.request and httpx implementations previously scattered
across manager.py and worker_handlers.py.
"""
from es.client import es_request, es_request_raw, get_es_client  # noqa: F401
