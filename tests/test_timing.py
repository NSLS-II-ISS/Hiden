import math

import pytest
from massoft_protocol import MASsoftProtocolError, parse_data_row
from massoft_timing import SourceGuard, csv_start_utc, parse_start_utc

START = 1_800_000_000.0


def guard(**kwargs):
    return SourceGuard(START, max_age=10, wall=START + 100, monotonic=100, **kwargs)


def push(g, milliseconds, values=(0.0, -5e-9), now=100):
    g.push(values, "raw", milliseconds, wall=START + now, monotonic=now)


def drain(g, now=100):
    return g.drain(wall=START + now, monotonic=now)


@pytest.mark.parametrize(
    "line,options,expected",
    [
        ("28:32:33\t102753536\t0\t-1e-10", {}, 102753536),
        ("0\t0\t-1e-10", {"include_ms": True}, 0),
        ("00:00:00\t0\t0\t-1e-10", {"include_ms": True}, 0),
        ("0\t-1e-10", {}, None),
    ],
)
def test_parser_preserves_time_without_changing_values(line, options, expected):
    values, elapsed = parse_data_row(line, expected_count=2, **options)
    assert values == [0, -1e-10]
    assert elapsed == expected


@pytest.mark.parametrize(
    "value", ["2026-10-01T12:00:00", "", "now", "2026-02-31T00:00:00Z", "1969-01-01T00:00:00Z"]
)
def test_origin_requires_valid_explicit_time_zone(value):
    with pytest.raises(ValueError):
        parse_start_utc(value)


def test_origin_converts_offset_without_host_timezone_guess():
    text, epoch = parse_start_utc("2026-09-29T16:49:05-04:00")
    assert text == "2026-09-29T20:49:05.000000Z"
    assert epoch == parse_start_utc(text)[1]
    with pytest.raises(ValueError, match="offset"):
        parse_start_utc("2026-09-29T16:49:05+00:99")


def test_history_is_counted_without_filling_live_queue():
    g = guard(capacity=2, not_before=START + 95)
    g.set_enabled(True)
    for i in range(95):
        push(g, i * 1000)
    assert drain(g) == []
    assert g.history_rows == 95
    assert g.snapshot()["queue_depth"] == 0
    assert g.dropped_rows == 0
    push(g, 98000)
    push(g, 99000, values=(1e-9, -2e-9))
    rows = drain(g)
    assert [r.elapsed_ms for r in rows] == [98000, 99000]
    assert rows[0].source_time == START + 98
    assert rows[0].receipt_time == START + 100
    assert rows[0].values == (0, -5e-9)


def test_no_timestamp_reversal_or_duplicate_publication():
    g = guard()
    g.set_enabled(True)
    push(g, 99000)
    assert len(drain(g)) == 1
    push(g, 99000)
    assert drain(g) == []
    assert g.duplicates == 1
    with pytest.raises(MASsoftProtocolError, match="backwards"):
        push(g, 98000)
    with pytest.raises(MASsoftProtocolError, match="backwards"):
        drain(g)


def test_same_counter_with_changed_values_is_not_deduplicated_silently():
    g = guard()
    push(g, 99000)
    with pytest.raises(MASsoftProtocolError, match="Different"):
        push(g, 99000, values=(1, 2))


def test_queue_overflow_latches_and_discards_pending_rows():
    g = guard(capacity=2)
    g.set_enabled(True)
    push(g, 97000)
    push(g, 98000)
    with pytest.raises(MASsoftProtocolError, match="overflow"):
        push(g, 99000)
    assert g.dropped_rows == 3
    assert g.snapshot()["queue_depth"] == 0
    with pytest.raises(MASsoftProtocolError, match="overflow"):
        drain(g)


def test_expired_queued_rows_are_counted_not_retimestamped():
    g = guard()
    g.set_enabled(True)
    push(g, 95000)
    assert drain(g, now=106) == []
    assert g.dropped_rows == 1


def test_drain_limit_retains_the_remaining_live_fifo():
    g = guard()
    g.set_enabled(True)
    for i in range(100):
        push(g, 99000 + i)
    assert len(drain(g)) == 64
    assert g.snapshot()["queue_depth"] == 36
    assert [row.elapsed_ms for row in drain(g)] == list(range(99064, 99100))
    assert g.dropped_rows == 0


def test_paused_stream_is_validated_without_queue_growth():
    g = guard(capacity=1)
    for i in range(20):
        push(g, (100 + i) * 1000, now=100 + i)
    assert g.snapshot()["queue_depth"] == 0
    assert g.dropped_rows == 0
    g.set_enabled(True, not_before=START + 120)
    push(g, 120000, now=120)
    push(g, 121000, now=121)
    assert [r.elapsed_ms for r in drain(g, now=121)] == [121000]


@pytest.mark.parametrize("jump", [-2, 2])
def test_clock_step_in_either_direction_latches(jump):
    g = guard()
    with pytest.raises(MASsoftProtocolError, match="clock stepped"):
        g.drain(wall=START + 101 + jump, monotonic=101)


def test_future_sample_fails_but_small_skew_waits_until_its_time():
    g = guard()
    g.set_enabled(True)
    push(g, 100500)
    assert drain(g) == []
    assert len(drain(g, now=100.5)) == 1
    with pytest.raises(MASsoftProtocolError, match="future"):
        push(g, 102000)


@pytest.mark.parametrize("counter", [None, -1, 3.4, True, 2**53 + 1])
def test_bad_counter_fails_closed(counter):
    with pytest.raises(MASsoftProtocolError, match="milliseconds"):
        push(guard(), counter)


@pytest.mark.parametrize("capacity", [0, -1, 1.5, True, 100001])
def test_invalid_capacity(capacity):
    with pytest.raises(ValueError):
        guard(capacity=capacity)


@pytest.mark.parametrize("age", [0, -1, math.nan, math.inf])
def test_invalid_age(age):
    with pytest.raises(ValueError):
        SourceGuard(START, max_age=age, wall=START, monotonic=0)


def test_csv_origin_is_offline_and_explicit_about_date_order_and_offset(tmp_path):
    path = tmp_path / "run.csv"
    contents = '"Date",9/29/2026,"Time",4:49:05 PM\n'
    path.write_text(contents)
    assert (
        csv_start_utc(path, date_order="mdy", utc_offset="-04:00") == "2026-09-29T20:49:05.000000Z"
    )
    assert path.read_text() == contents
    with pytest.raises(ValueError):
        csv_start_utc(path, date_order="dmy", utc_offset="-04:00")
    path.write_text('"Date",01/10/2026,"Time",12:00:00\n')
    assert csv_start_utc(path, date_order="dmy", utc_offset="-04:00").startswith(
        "2026-10-01T16:00:00"
    )
    with pytest.raises(ValueError):
        csv_start_utc(path, date_order="mdy", utc_offset="EDT")
    path.write_text("not a MASsoft header\n")
    with pytest.raises(ValueError, match="header"):
        csv_start_utc(path, date_order="mdy", utc_offset="-04:00")
