"""AST synchronization for Flume codebase graph.

Phase 7: Migrated from urllib.request to shared es/client.py.
"""
import ast
import os
from pathlib import Path

from utils.logger import get_logger
from es.client import es_request

logger = get_logger("elastro_sync")


def sync_ast():
    """Robust fallback traversing AST scopes natively exporting boundaries to ES"""
    for filepath in Path("/app/src").rglob("*.py"):
        try:
            tree = ast.parse(filepath.read_text())
            classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
            payload = {"file": str(filepath.name), "classes": classes}
            try:
                es_request('/flume-elastro-graph/_doc', body=payload, method='POST')
            except Exception as e:
                logger.warning(
                    "Failed to write AST entry to ES",
                    extra={"structured_data": {"file": str(filepath.name), "error": str(e)}},
                )
        except Exception as e:
            logger.warning(
                "Failed to parse AST for file",
                extra={"structured_data": {"file": str(filepath), "error": str(e)}},
            )
