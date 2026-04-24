"""
Integration tests for Elasticsearch bindings.

Validates real ES read/write operations against the running Flume stack.
These tests verify that the dashboard's ES indices exist, accept writes,
and return documents with expected shapes.

Requires: Flume stack running (./flume start)
"""
import uuid
import pytest


@pytest.mark.integration
class TestElasticsearchHealth:
    """Validates the Elasticsearch cluster is healthy and accessible."""

    def test_cluster_health_green_or_yellow(self, es_client):
        """ES cluster should be at least yellow (single-node has no replicas)."""
        resp = es_client.get("/_cluster/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("green", "yellow")
        assert data["number_of_nodes"] >= 1

    def test_cluster_info(self, es_client):
        """ES should return version and cluster information."""
        resp = es_client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "number" in data["version"]


@pytest.mark.integration
class TestFlumeIndices:
    """Validates that Flume's core ES indices exist and are operational."""

    CORE_INDICES = [
        "agent-plan-sessions",
        "agent-task-records",
        "agent-system-workers",
    ]

    @pytest.mark.parametrize("index", CORE_INDICES)
    def test_index_exists(self, es_client, index):
        """Core Flume indices must exist after stack bootstrap."""
        resp = es_client.head(f"/{index}")
        # 200 = exists, 404 = doesn't exist
        assert resp.status_code == 200, f"Index '{index}' does not exist"

    @pytest.mark.parametrize("index", CORE_INDICES)
    def test_index_accepts_search(self, es_client, index):
        """Each core index should accept a basic _search request."""
        resp = es_client.post(
            f"/{index}/_search",
            json={"size": 0, "query": {"match_all": {}}},
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "hits" in data
        assert "total" in data["hits"]


@pytest.mark.integration
class TestDocumentLifecycle:
    """Validates the full document write/read/delete cycle in ES."""

    TEST_INDEX = "flume-integration-test"

    def test_index_write_read_delete(self, es_client):
        """Full CRUD lifecycle: create index, write doc, read it back, delete."""
        doc_id = f"test-{uuid.uuid4().hex[:8]}"
        doc = {
            "name": "integration-test-doc",
            "created_at": "2026-04-24T00:00:00Z",
            "value": 42,
        }

        # Write
        resp = es_client.put(
            f"/{self.TEST_INDEX}/_doc/{doc_id}?refresh=true",
            json=doc,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code in (200, 201)
        write_data = resp.json()
        assert write_data["result"] in ("created", "updated")

        # Read back
        resp = es_client.get(f"/{self.TEST_INDEX}/_doc/{doc_id}")
        assert resp.status_code == 200
        read_data = resp.json()
        assert read_data["_source"]["name"] == "integration-test-doc"
        assert read_data["_source"]["value"] == 42

        # Delete document
        resp = es_client.delete(f"/{self.TEST_INDEX}/_doc/{doc_id}?refresh=true")
        assert resp.status_code == 200
        assert resp.json()["result"] == "deleted"

        # Cleanup: delete test index
        es_client.delete(f"/{self.TEST_INDEX}")


@pytest.mark.integration
class TestSessionsPersistence:
    """Validates that plan sessions can be written to and read from ES."""

    def test_session_roundtrip(self, es_client):
        """Write a session doc, read it back, verify shape, delete."""
        session_id = f"test-session-{uuid.uuid4().hex[:8]}"
        session = {
            "id": session_id,
            "repo": "test-repo",
            "prompt": "Build a todo app",
            "messages": [],
            "draftPlan": None,
            "planningStatus": {"stage": "queued"},
            "created_at": "2026-04-24T00:00:00Z",
            "updated_at": "2026-04-24T00:00:00Z",
        }

        # Write
        resp = es_client.put(
            f"/agent-plan-sessions/_doc/{session_id}?refresh=true",
            json=session,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code in (200, 201)

        # Read via _search (mimicking load_session)
        resp = es_client.post(
            "/agent-plan-sessions/_search",
            json={"size": 1, "query": {"term": {"_id": session_id}}},
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        hits = resp.json()["hits"]["hits"]
        assert len(hits) == 1
        src = hits[0]["_source"]
        assert src["id"] == session_id
        assert src["repo"] == "test-repo"
        assert src["planningStatus"]["stage"] == "queued"

        # Cleanup
        es_client.delete(f"/agent-plan-sessions/_doc/{session_id}?refresh=true")
