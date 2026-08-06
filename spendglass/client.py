"""Redbark API client — the ONLY module that touches the network or the key.

Every endpoint is a GET; this client physically cannot mutate anything
upstream. Rate limits are respected client-side (docs.redbark.com):
cheap tier (connections/accounts/categories) 60/min, mid (balances/
account-details) 30/min, heavy (transactions/holdings/trades) 30/min with
a 4-concurrent cap. Sync runs sequentially, so the concurrency cap is
satisfied by construction; the per-minute limits are enforced with a
minimum interval per tier.

429s honour Retry-After; 5xx retries with backoff. 403 on holdings/trades
raises PlanGated (Professional-plan endpoints) so sync can degrade
gracefully instead of failing the whole run.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterator

import httpx

CHEAP = "cheap"   # /connections /accounts /categories        60/min
MID = "mid"       # /balances /account-details                 30/min
HEAVY = "heavy"   # /transactions /holdings /trades            30/min, 4 concurrent

_TIER_MIN_INTERVAL = {CHEAP: 60 / 60, MID: 60 / 30, HEAVY: 60 / 30}

PAGE_LIMIT = 500  # transactions max per page; other endpoints tolerate less


class RedbarkError(RuntimeError):
    """A non-retryable API failure (4xx other than 429/403-plan)."""


class PlanGated(RedbarkError):
    """403 on a Professional-plan endpoint (holdings/trades)."""


class RedbarkClient:
    def __init__(
        self,
        api_key: str,
        api_url: str = "https://api.redbark.com",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not api_key:
            raise RedbarkError("REDBARK_API_KEY is not set — copy .env.example to .env")
        self._http = httpx.Client(
            base_url=api_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
            transport=transport,
        )
        self._sleep = sleep
        self._clock = clock
        self._last_call: dict[str, float] = {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "RedbarkClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── plumbing ────────────────────────────────────────────────────────────

    def _throttle(self, tier: str) -> None:
        min_interval = _TIER_MIN_INTERVAL[tier]
        last = self._last_call.get(tier)
        if last is not None:
            wait = min_interval - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_call[tier] = self._clock()

    def _get(self, path: str, tier: str, params: dict[str, Any] | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(4):
            self._throttle(tier)
            resp = self._http.get(path, params=params)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", 5))
                self._sleep(retry_after)
                continue
            if resp.status_code >= 500:
                self._sleep(2**attempt)
                continue
            if resp.status_code == 403 and path in ("/v1/holdings", "/v1/trades"):
                raise PlanGated(f"{path} requires the Professional plan (403)")
            if resp.status_code >= 400:
                raise RedbarkError(f"GET {path} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise RedbarkError(f"GET {path}: retries exhausted")

    def _paginate(self, path: str, tier: str, params: dict[str, Any]) -> Iterator[dict]:
        """Yield every item across offset pages until hasMore is false.

        Endpoints without a pagination envelope return everything in one
        page; the loop exits after it.
        """
        offset = 0
        while True:
            page = self._get(path, tier, {**params, "limit": PAGE_LIMIT, "offset": offset})
            items = page.get("data", [])
            yield from items
            pagination = page.get("pagination") or {}
            if not pagination.get("hasMore"):
                return
            offset += len(items)
            if not items:  # defensive: hasMore true with an empty page
                return

    # ── the eight endpoints ─────────────────────────────────────────────────

    def connections(self) -> list[dict]:
        return self._get("/v1/connections", CHEAP).get("data", [])

    def accounts(self) -> list[dict]:
        return list(self._paginate("/v1/accounts", CHEAP, {}))

    def categories(self) -> list[dict]:
        return self._get("/v1/categories", CHEAP).get("categories", [])

    def balances(self, account_ids: list[str]) -> list[dict]:
        # REST accepts up to 100 ids per call; chunk conservatively at 50.
        out: list[dict] = []
        for i in range(0, len(account_ids), 50):
            chunk = account_ids[i : i + 50]
            out.extend(
                self._get("/v1/balances", MID, {"accountIds": ",".join(chunk)}).get("data", [])
            )
        return out

    def account_details(self, account_ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(account_ids), 50):
            chunk = account_ids[i : i + 50]
            out.extend(
                self._get("/v1/account-details", MID, {"accountIds": ",".join(chunk)}).get(
                    "data", []
                )
            )
        return out

    def transactions(
        self,
        connection_id: str,
        account_id: str,
        from_date: str,
        to_date: str | None = None,
        include_pending: bool = True,
    ) -> Iterator[dict]:
        # accountId is required (all-accounts form is 410 after 2026-06-30).
        return self._paginate(
            "/v1/transactions",
            HEAVY,
            {
                "connectionId": connection_id,
                "accountId": account_id,
                "from": from_date,
                "to": to_date,
                "includePending": "true" if include_pending else "false",
            },
        )

    def holdings(self, connection_id: str, account_id: str | None = None) -> list[dict]:
        return self._get(
            "/v1/holdings", HEAVY, {"connectionId": connection_id, "accountId": account_id}
        ).get("data", [])

    def trades(
        self,
        connection_id: str,
        account_id: str,
        from_date: str,
        to_date: str,
    ) -> Iterator[dict]:
        # Caller (sync.py) is responsible for keeping from/to within the
        # API's 366-day window; this just pages through one window.
        return self._paginate(
            "/v1/trades",
            HEAVY,
            {
                "connectionId": connection_id,
                "accountId": account_id,
                "from": from_date,
                "to": to_date,
            },
        )
