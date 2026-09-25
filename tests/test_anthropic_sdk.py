"""The installed Anthropic SDK accepts every request the miners make.

The other miner tests hand in a stand-in client, so they would still pass
if an SDK release dropped a parameter spendglass sends. Here the real SDK
builds, sends and parses each call against a fake API served by
httpx2.MockTransport. Keyless and offline like the rest of the suite.
"""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from spendglass import enrich, lookup
from spendglass.store import Store
from tests.conftest import ACCOUNTS, CATEGORIES, CONNECTIONS, TXNS

BATCH_ID = "msgbatch_test"


def _message(text: str) -> dict:
    return {"id": "msg_test", "type": "message", "role": "assistant",
            "model": "claude-test", "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}


def _batch(status: str) -> dict:
    return {"id": BATCH_ID, "type": "message_batch", "processing_status": status,
            "request_counts": {"processing": 0, "succeeded": 1, "errored": 0,
                               "canceled": 0, "expired": 0},
            "created_at": "2026-09-01T00:00:00Z",
            "expires_at": "2026-09-02T00:00:00Z", "ended_at": None,
            "archived_at": None, "cancel_initiated_at": None,
            "results_url": ("https://api.anthropic.com/v1/messages/batches/"
                            f"{BATCH_ID}/results")}


class FakeAnthropicAPI:
    """Just enough of Messages and Message Batches, with a request log."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[tuple[httpx2.Request, dict | None]] = []
        self.batch_requests: list[dict] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request, body))
        path = request.url.path
        if path == "/v1/messages":
            return httpx2.Response(200, json=_message(self.reply))
        if path == "/v1/messages/batches":
            self.batch_requests = body["requests"]
            return httpx2.Response(200, json=_batch("in_progress"))
        if path == f"/v1/messages/batches/{BATCH_ID}":
            return httpx2.Response(200, json=_batch("ended"))
        if path == f"/v1/messages/batches/{BATCH_ID}/results":
            lines = [json.dumps({"custom_id": r["custom_id"], "result": {
                "type": "succeeded", "message": _message(self.reply)}})
                for r in self.batch_requests]
            return httpx2.Response(200, content="\n".join(lines).encode())
        return httpx2.Response(404, json={"type": "error", "error": {
            "type": "not_found_error", "message": path}})

    def client(self) -> anthropic.Anthropic:
        # base_url is explicit so an ANTHROPIC_BASE_URL in the shell can't
        # change what the test sees.
        return anthropic.Anthropic(
            api_key="test-key-not-real", base_url="https://api.anthropic.com",
            max_retries=0,
            http_client=anthropic.DefaultHttpxClient(
                transport=httpx2.MockTransport(self.handler)))


@pytest.fixture()
def store(tmp_path):
    with Store(tmp_path / "store.db") as s:
        s.upsert_connections(CONNECTIONS)
        s.upsert_accounts(ACCOUNTS)
        s.upsert_categories(CATEGORIES)
        s.upsert_transactions(TXNS, "conn-bank-1")
        enrich.rebuild_merchants(s)
        yield s


def test_classification_batch_round_trips(store):
    api = FakeAnthropicAPI(json.dumps({"category": "EATING_OUT",
                                       "confidence": 0.9}))
    out = enrich.classify_merchants(store, api.client(), poll_seconds=0)
    assert out["status"] == "ok" and out["classified"] >= 1
    assert out["errored"] == 0 and out["invalid"] == 0
    params = api.batch_requests[0]["params"]
    assert params["model"] == enrich.MODEL
    assert params["output_config"]["format"]["type"] == "json_schema"


def test_web_lookup_round_trips(store):
    api = FakeAnthropicAPI(json.dumps({
        "merchant_name": "AcmeCo", "summary": "Hardware store in Fairhaven",
        "subcategory": "Hardware", "confidence": 0.9, "evidence_url": None}))
    out = lookup.lookup_merchants(store, api.client(), limit=1, workers=1)
    assert out["errored"] == 0 and out["auto"] == 1
    request, body = api.calls[0]
    assert request.url.params.get("beta") == "true"
    assert "server-side-fallback-2026-07-01" in request.headers["anthropic-beta"]
    assert body["model"] == lookup.MODEL
    assert body["fallbacks"] == "default"
    assert body["tools"][0]["type"] == "web_search_20260209"


def test_identity_sweep_batch_round_trips(store):
    api = FakeAnthropicAPI(json.dumps({"name": "AcmeCo",
                                       "subcategory": "Hardware",
                                       "confidence": 0.9}))
    out = lookup.identify_clear_merchants(store, api.client(), poll_seconds=0)
    assert out["status"] == "ok" and out["errored"] == 0
    assert out["auto"] >= 1
    assert api.batch_requests[0]["params"]["output_config"]["format"]


def test_decision_propagation_round_trips(store):
    lookup.ensure_schema(store)
    store.con.execute(
        """INSERT INTO merchant_lookups (merchant_key, proposed_name,
           confidence, status) VALUES ('acmeco north', '', 0.2, 'pending')""")
    store.con.commit()
    api = FakeAnthropicAPI(json.dumps([{
        "merchant_key": "acmeco north", "name": "AcmeCo",
        "subcategory": "Hardware", "reason": "same descriptor pattern",
        "confidence": 0.8}]))
    n = lookup.propagate_decisions(store, api.client(), [
        {"merchant_key": "acmeco south", "name": "AcmeCo",
         "subcategory": "Hardware"}])
    assert n == 1
    request, body = api.calls[0]
    assert "beta" not in request.url.params
    assert body["model"] == "claude-haiku-4-5"
