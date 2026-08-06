"""Scheduler due-logic — pure arithmetic, no subprocesses in tests."""

from spendglass.autosync import due
from spendglass.lookup import SETTING_DEFAULTS

NOW = 1_800_000_000.0  # any fixed epoch


def _iso(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def test_never_synced_is_due_immediately():
    assert due(None, 6, NOW)


def test_zero_interval_disables_even_when_never_synced():
    assert not due(None, 0, NOW)
    assert not due(_iso(NOW - 999_999), 0, NOW)


def test_due_exactly_at_interval_boundary():
    assert not due(_iso(NOW - 6 * 3600 + 1), 6, NOW)
    assert due(_iso(NOW - 6 * 3600), 6, NOW)


def test_unparseable_timestamp_counts_as_never():
    assert due("not-a-date", 6, NOW)


def test_interval_setting_has_a_default():
    assert SETTING_DEFAULTS["sync_interval_hours"] == 6
