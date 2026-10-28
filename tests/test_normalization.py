from datetime import date
import pytest

from app.normalization import number, date_from_epoch, daily_window, normalize_daily

TODAY = date(2026, 10, 29)


def stamp(day, hour=0):
    from datetime import datetime, timezone
    return datetime(2026, 10, day, hour, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("value", [None, True, "12", float("nan"), float("inf"), -float("inf"), 10 ** 1000])
def test_non_numeric_or_non_finite_is_unavailable(value):
    assert number(value) is None


def test_zero_is_available():
    assert number(0) == 0
    assert number(2.5) == 2.5


@pytest.mark.parametrize("value", [None, True, "bad", float("nan"), float("inf"), 10 ** 1000])
def test_invalid_dates_do_not_crash(value):
    assert date_from_epoch(value) is None


def test_completed_window_has_exact_days():
    assert daily_window(2, today=TODAY) == {
        "start": "2026-10-27", "end": "2026-10-28", "timezone": "UTC",
        "includes_partial_day": False,
    }
    assert daily_window(2, True, today=TODAY)["start"] == "2026-10-28"


@pytest.mark.parametrize("kind", ["tvl", "fees"])
def test_unsorted_duplicate_dates_keep_latest_timestamp_not_sum(kind):
    pairs = [[stamp(28, 12), 120], [stamp(27), 80], [stamp(28), 100], [stamp(29), 999]]
    rows = [{"date": t, "totalLiquidityUSD": v} for t, v in pairs] if kind == "tvl" else pairs
    points, window, coverage, warnings = normalize_daily(rows, kind, 2, today=TODAY)
    field = "tvl_usd" if kind == "tvl" else "value_usd"
    assert points == [{"date": "2026-10-27", field: 80}, {"date": "2026-10-28", field: 120}]
    assert coverage["missing_days"] == []
    assert coverage["duplicate_observations"] == 1
    assert any("incomplete" in w for w in warnings)


def test_equal_timestamp_keeps_last_occurrence_and_reports_gap():
    points, _, coverage, warnings = normalize_daily(
        [[stamp(28), 0], [stamp(28), 11]], "fees", 2, today=TODAY)
    assert points == [{"date": "2026-10-28", "value_usd": 11}]
    assert coverage["missing_days"] == ["2026-10-27"]
    assert any("not summed" in w for w in warnings)


def test_current_day_requires_opt_in_and_future_is_excluded():
    points, window, _, warnings = normalize_daily(
        [[stamp(28), 1], [stamp(29), 2], [stamp(30), 3]], "fees", 2, True, today=TODAY)
    assert len(points) == 2
    assert window["includes_partial_day"]
    assert any("future" in w for w in warnings)


def test_invalid_observations_are_counted_without_zero_fill():
    points, _, coverage, warnings = normalize_daily(
        [[stamp(28), float("nan")], ["bad", 1], None, [stamp(27), 0]],
        "fees", 2, today=TODAY)
    assert points == [{"date": "2026-10-27", "value_usd": 0}]
    assert coverage["invalid_observations"] == 3
    assert coverage["missing_days"] == ["2026-10-28"]


def test_wrong_history_container_is_rejected():
    with pytest.raises(ValueError):
        normalize_daily({"unexpected": "object"}, "tvl", 2, today=TODAY)
