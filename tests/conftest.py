"""Shared fixtures — a fake Redbark API served by httpx.MockTransport.

Everything runs KEYLESS and offline: the fake key never leaves the process
and no test can reach the network (MockTransport intercepts everything).
"""

from __future__ import annotations

import json

import httpx
import pytest

from spendglass.client import RedbarkClient
from spendglass.store import Store

FAKE_KEY = "rbk_test_" + "0" * 64  # shape-valid, obviously synthetic

CONNECTIONS = [
    {
        "id": "conn-bank-1", "provider": "fiskil", "category": "banking",
        "institutionId": "inst-1", "institutionName": "Example Bank",
        "institutionLogo": None, "status": "active",
        "lastRefreshedAt": "2026-08-01T00:00:00Z", "createdAt": "2026-01-15T00:00:00Z",
    },
    {
        "id": "conn-broker-1", "provider": "snaptrade", "category": "brokerage",
        "institutionId": "inst-2", "institutionName": "Example Broker",
        "institutionLogo": None, "status": "active",
        "lastRefreshedAt": "2026-08-01T00:00:00Z", "createdAt": "2026-06-01T00:00:00Z",
    },
    {
        "id": "conn-dead-1", "provider": "fiskil", "category": "banking",
        "institutionId": "inst-3", "institutionName": "Lapsed Bank",
        "institutionLogo": None, "status": "invalidated",
        "lastRefreshedAt": "2026-05-01T00:00:00Z", "createdAt": "2025-04-01T00:00:00Z",
    },
]

ACCOUNTS = [
    {"id": "acc-1", "connectionId": "conn-bank-1", "provider": "fiskil",
     "name": "Everyday", "type": "transaction", "institutionName": "Example Bank",
     "accountNumber": "xxx-123", "currency": "AUD"},
    {"id": "acc-2", "connectionId": "conn-broker-1", "provider": "snaptrade",
     "name": "Shares", "type": "brokerage", "institutionName": "Example Broker",
     "accountNumber": None, "currency": "AUD"},
    {"id": "acc-dead", "connectionId": "conn-dead-1", "provider": "fiskil",
     "name": "Old", "type": "transaction", "institutionName": "Lapsed Bank",
     "accountNumber": None, "currency": "AUD"},
]

TXNS = [
    {"id": "txn-1", "accountId": "acc-1", "accountName": "Everyday",
     "status": "posted", "date": "2026-07-30", "datetime": "2026-07-30T09:00:00Z",
     "postDate": "2026-07-31", "postDatetime": None, "valueDate": None,
     "valueDatetime": None, "description": "COFFEE CO EXAMPLEVILLE",
     "amount": "-4.50", "direction": "debit", "category": "EATING_OUT",
     "customCategory": None, "customCategoryGroup": None,
     "merchantName": "Coffee Co", "merchantCategoryCode": "5814"},
    {"id": "txn-2", "accountId": "acc-1", "accountName": "Everyday",
     "status": "pending", "date": "2026-08-01", "datetime": "2026-08-01T12:00:00Z",
     "postDate": None, "postDatetime": None, "valueDate": None,
     "valueDatetime": None, "description": "SALARY",
     "amount": "1000.00", "direction": "credit", "category": "INCOME",
     "customCategory": None, "customCategoryGroup": None,
     "merchantName": None, "merchantCategoryCode": None},
]

HOLDINGS = [
    {"id": "hold-1", "accountId": "acc-2", "accountName": "Shares",
     "symbol": "VAS", "name": "Vanguard AUS", "exchange": "ASX", "currency": "AUD",
     "quantity": "10", "averagePrice": "88.00", "currentPrice": "95.50",
     "marketValue": "955.00", "unrealizedPnl": "75.00"},
]

TRADES = [
    {"id": "snap_trade-1", "accountId": "acc-2", "accountName": "Shares",
     "symbol": "VAS", "name": "Vanguard AUS", "type": "buy", "quantity": "10",
     "price": "88.00", "currency": "AUD", "totalAmount": "880.00", "fees": "9.50",
     "tradeDate": "2026-03-10", "settlementDate": "2026-03-12",
     "description": None},
]

BALANCES = [
    {"accountId": "acc-1", "currentBalance": "2500.10",
     "availableBalance": "2400.00", "currency": "AUD"},
    {"accountId": "acc-2", "currentBalance": "955.00",
     "availableBalance": None, "currency": "AUD"},
]

CATEGORIES = [{"key": "EATING_OUT", "label": "Eating out"},
              {"key": "INCOME", "label": "Income"}]


def _paged(items: list[dict]) -> dict:
    return {"data": items,
            "pagination": {"total": len(items), "limit": 500, "offset": 0,
                           "hasMore": False}}


class FakeAPI:
    """Route table + call log, so tests can assert on request shapes."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.plan_gated = False  # flip to make holdings/trades 403

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if request.headers.get("authorization") != f"Bearer {FAKE_KEY}":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        if path == "/v1/connections":
            return httpx.Response(200, json={"data": CONNECTIONS})
        if path == "/v1/accounts":
            return httpx.Response(200, json=_paged(ACCOUNTS))
        if path == "/v1/categories":
            return httpx.Response(200, json={"categories": CATEGORIES})
        if path == "/v1/balances":
            return httpx.Response(200, json={"data": BALANCES})
        if path == "/v1/transactions":
            params = dict(request.url.params)
            assert "connectionId" in params and "accountId" in params, (
                "accountId is REQUIRED (all-accounts form 410s after 2026-06-30)"
            )
            items = [t for t in TXNS if t["accountId"] == params["accountId"]]
            return httpx.Response(200, json=_paged(items))
        if path == "/v1/holdings":
            if self.plan_gated:
                return httpx.Response(403, json={"error": {"message": "plan"}})
            return httpx.Response(200, json={"data": HOLDINGS})
        if path == "/v1/trades":
            if self.plan_gated:
                return httpx.Response(403, json={"error": {"message": "plan"}})
            return httpx.Response(200, json=_paged(TRADES))
        return httpx.Response(404, json={"error": {"message": f"no route {path}"}})


@pytest.fixture()
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture()
def client(api: FakeAPI) -> RedbarkClient:
    c = RedbarkClient(
        FAKE_KEY,
        transport=httpx.MockTransport(api.handler),
        sleep=lambda s: None,   # no real sleeping in tests
    )
    yield c
    c.close()


@pytest.fixture()
def store(tmp_path) -> Store:
    s = Store(tmp_path / "store.db")
    yield s
    s.close()
