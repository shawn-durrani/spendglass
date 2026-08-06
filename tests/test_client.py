"""Client behavior: auth, throttling, retries, pagination, plan gating."""

import httpx
import pytest

from spendglass.client import CHEAP, PlanGated, RedbarkClient, RedbarkError
from tests.conftest import FAKE_KEY


def test_requires_key():
    with pytest.raises(RedbarkError, match="REDBARK_API_KEY"):
        RedbarkClient("")


def test_sends_bearer_auth(client, api):
    client.connections()
    assert api.calls[0].headers["authorization"] == f"Bearer {FAKE_KEY}"


def test_throttle_spaces_same_tier_calls():
    """Two cheap-tier calls must be >= 1s apart on the fake clock."""
    sleeps: list[float] = []
    fake_now = [0.0]

    def sleep(s: float) -> None:
        sleeps.append(s)
        fake_now[0] += s

    def clock() -> float:
        return fake_now[0]

    c = RedbarkClient(
        FAKE_KEY,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []})),
        sleep=sleep,
        clock=clock,
    )
    c.connections()
    c.connections()  # immediate second call — must be delayed
    c.close()
    assert sleeps and abs(sum(sleeps) - 1.0) < 0.01  # 60/min => 1s interval


def test_429_honours_retry_after():
    sleeps: list[float] = []
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(429, headers={"retry-after": "7"}, json={})
        return httpx.Response(200, json={"data": [{"id": "c1"}]})

    c = RedbarkClient(FAKE_KEY, transport=httpx.MockTransport(handler),
                      sleep=sleeps.append)
    result = c.connections()
    c.close()
    assert result == [{"id": "c1"}]
    assert 7.0 in sleeps  # the Retry-After value, not a guess


def test_5xx_retries_then_gives_up():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    c = RedbarkClient(FAKE_KEY, transport=httpx.MockTransport(handler),
                      sleep=lambda s: None)
    with pytest.raises(RedbarkError, match="retries exhausted"):
        c.connections()
    c.close()


def test_4xx_raises_immediately():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad param"}})

    c = RedbarkClient(FAKE_KEY, transport=httpx.MockTransport(handler),
                      sleep=lambda s: None)
    with pytest.raises(RedbarkError, match="400"):
        c.connections()
    c.close()


def test_holdings_403_raises_plan_gated(client, api):
    api.plan_gated = True
    with pytest.raises(PlanGated):
        client.holdings("conn-broker-1", "acc-2")


def test_pagination_walks_all_pages():
    """3 items served one per page; the client must collect all of them."""
    items = [{"id": f"a-{i}"} for i in range(3)]

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        page = items[offset : offset + 1]
        return httpx.Response(200, json={
            "data": page,
            "pagination": {"total": 3, "limit": 1, "offset": offset,
                           "hasMore": offset + 1 < 3},
        })

    c = RedbarkClient(FAKE_KEY, transport=httpx.MockTransport(handler),
                      sleep=lambda s: None)
    assert c.accounts() == items
    c.close()


def test_transactions_always_sends_account_id(client, api):
    list(client.transactions("conn-bank-1", "acc-1", "2026-07-01"))
    params = dict(api.calls[-1].url.params)
    assert params["accountId"] == "acc-1"
    assert params["connectionId"] == "conn-bank-1"
    assert params["includePending"] == "true"
