"""Tests for the Flask app routes (excluding LLM-dependent flows).

The voice upload route hits the full pipeline — covered indirectly via
test_voice_pipeline. These tests focus on the verify + stats surface.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def app(backend):
    from sponge.app import create_app
    app = create_app(backend=backend)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_home_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Sponge" in resp.data


def test_verify_pane_empty_state(client):
    resp = client.get("/verify")
    assert resp.status_code == 200
    # Empty backend → "all clear" copy
    assert b"All clear" in resp.data


def test_verify_pane_lists_provisional(client, backend):
    backend.add_node(
        {"id": "person_alice", "label": "Alice", "file_type": "person"},
        provisional_source="voice_memo:abc",
    )
    resp = client.get("/verify")
    assert resp.status_code == 200
    assert b"voice_memo:abc" in resp.data
    assert b"Alice" in resp.data


def test_stats_endpoint(client, backend):
    backend.add_node({"id": "n1", "label": "A", "file_type": "concept"})
    resp = client.get("/api/sponge/stats")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["nodes"] == 1
    assert payload["edges"] == 0


def test_dashboard_alias_returns_same_payload(client, backend):
    backend.add_node({"id": "n1", "label": "A", "file_type": "concept"})
    resp = client.get("/api/sponge/dashboard")
    assert resp.status_code == 200
    assert resp.get_json()["nodes"] == 1


def test_topics_endpoint_returns_empty_for_v01(client):
    resp = client.get("/api/sponge/topics")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "topics": []}


def test_apply_flips_provisional(client, backend):
    backend.add_node(
        {"id": "n1", "label": "A", "file_type": "person"},
        provisional_source="voice_memo:abc",
    )
    resp = client.post(
        "/api/verify/apply",
        json={"provisional_source": "voice_memo:abc"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["flipped"] == 1
    n = backend.get_node("n1")
    assert n["verified"] is True


def test_reject_removes_provisional(client, backend):
    backend.add_node(
        {"id": "n1", "label": "A", "file_type": "person"},
        provisional_source="voice_memo:abc",
    )
    resp = client.post(
        "/api/verify/reject",
        json={"provisional_source": "voice_memo:abc"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["removed"] >= 1
    assert backend.get_node("n1") is None


def test_apply_requires_source(client):
    resp = client.post("/api/verify/apply", json={})
    assert resp.status_code == 400


def test_apply_batch_flips_all_with_prefix(client, backend):
    backend.add_node(
        {"id": "n1", "label": "A", "file_type": "person"},
        provisional_source="voice_memo:abc",
    )
    backend.add_node(
        {"id": "n2", "label": "B", "file_type": "person"},
        provisional_source="voice_memo:xyz",
    )
    resp = client.post(
        "/api/verify/apply_batch",
        json={"prefix": "voice_memo:"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["flipped"] == 2


def test_voice_job_endpoint_returns_501(client):
    """v0.1 doesn't ship async voice jobs."""
    resp = client.get("/api/voice/job/anything")
    assert resp.status_code == 501


def test_audio_upload_requires_file(client):
    resp = client.post("/api/inbox/audio_upload", data={})
    assert resp.status_code == 400


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
