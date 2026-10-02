import math

import pytest
from massoft_protocol import MASsoftProtocolError, parse_data_row, parse_realtime_row
from massoft_timing import MASsoftClock, SourceGuard, csv_start_utc, parse_start_utc

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


REAL_ROW = (
    "10/1/2026 3:51:41 PM\t43\t4.70849e-09\t0\t8.6e-12\t6.38227e-11\t0"
    "\t1.12905e-08\t-2.6875e-12\t1.2255e-11\t-2.68749e-09\t0"
)


def test_reported_view2_row_preserves_columns_and_does_not_invent_subseconds():
    values, milliseconds, text = parse_realtime_row(REAL_ROW, expected_count=10)
    assert values == [
        4.70849e-9,
        0,
        8.6e-12,
        6.38227e-11,
        0,
        1.12905e-8,
        -2.6875e-12,
        1.2255e-11,
        -2.68749e-9,
        0,
    ]
    assert milliseconds == 43
    epoch, resolution = MASsoftClock("America/New_York", "mdy").parse(text)
    assert epoch == parse_start_utc("2026-10-01T19:51:41Z")[1]
    assert resolution == 1


@pytest.mark.parametrize(
    "text,expected,resolution",
    [
        ("10/1/2026 12:00:00 AM", "2026-10-01T04:00:00Z", 1),
        ("10/1/2026 12:00:00 PM", "2026-10-01T16:00:00Z", 1),
        ("10/1/2026 15:51:41.125", "2026-10-01T19:51:41.125Z", 0.001),
        ("10/1/2026 3:51:41.12 pm", "2026-10-01T19:51:41.12Z", 0.01),
        ("1/1/2026 00:00:00", "2026-01-01T05:00:00Z", 1),
        ("10/2/2026 00:00:00", "2026-10-02T04:00:00Z", 1),
    ],
)
def test_mas_clock_am_pm_fraction_timezone_and_midnight(text, expected, resolution):
    epoch, actual_resolution = MASsoftClock("America/New_York", "mdy").parse(text)
    assert epoch == parse_start_utc(expected)[1]
    assert actual_resolution == resolution


def test_date_order_is_explicit_not_guessed():
    text = "01/10/2026 15:51:41"
    epoch, _ = MASsoftClock("America/New_York", "dmy").parse(text)
    assert epoch == parse_start_utc("2026-10-01T19:51:41Z")[1]
    epoch, _ = MASsoftClock("America/New_York", "mdy").parse(text)
    assert epoch == parse_start_utc("2026-01-10T20:51:41Z")[1]


@pytest.mark.parametrize("text", ["11/1/2026 1:30:00 AM", "3/8/2026 2:30:00 AM"])
def test_dst_ambiguity_and_nonexistent_local_time_fail_closed(text):
    with pytest.raises(MASsoftProtocolError, match="DST"):
        MASsoftClock("America/New_York", "mdy").parse(text)


@pytest.mark.parametrize(
    "text",
    [
        "00:00:00",
        "10/1/2026 00:00:00 AM",
        "2/30/2026 12:00:00",
        "10/1/26 15:51:41",
        "10/1/2026 15:51:41.1234567",
        "garbage",
    ],
)
def test_bad_real_time_fails_without_receipt_time_fallback(text):
    with pytest.raises(MASsoftProtocolError):
        MASsoftClock("America/New_York", "mdy").parse(text)


def test_invalid_clock_configuration():
    with pytest.raises(ValueError, match="date_order"):
        MASsoftClock("UTC", "auto")
    with pytest.raises(ValueError, match="timezone"):
        MASsoftClock("Invalid/Zone", "mdy")


@pytest.mark.parametrize(
    "row",
    [
        "00:00:00\t43\t0\t1",
        "43\t0\t1",
        "10/1/2026 3:51:41 PM\t43\t0\t",
        "10/1/2026 3:51:41 PM\t43\t0\tnan",
        "10/1/2026 3:51:41 PM\t0\t1",
    ],
)
def test_realtime_shape_must_match_mass_count(row):
    with pytest.raises(MASsoftProtocolError):
        parse_realtime_row(row, expected_count=2)


def realtime_guard():
    clock = MASsoftClock("America/New_York", "mdy")
    now, _ = clock.parse("10/1/2026 3:52:00 PM")
    g = SourceGuard(None, clock=clock, max_age=10, wall=now, monotonic=100)
    g.set_enabled(True)
    return g, now


def test_real_time_guard_filters_history_without_any_start_reference():
    g, now = realtime_guard()
    values, ms, text = parse_realtime_row(REAL_ROW, expected_count=10)
    g.push(values, REAL_ROW, ms, source_text=text, wall=now, monotonic=100)
    assert g.history_rows == 1
    assert g.drain(wall=now, monotonic=100) == []
    # Do not assume the raw ms field is an elapsed counter or fractional seconds.
    g.push(values, "live", 12, source_text="10/1/2026 3:51:59 PM", wall=now, monotonic=100)
    rows = g.drain(wall=now, monotonic=100)
    assert len(rows) == 1
    assert rows[0].source_time == now - 1
    assert rows[0].source_text == "10/1/2026 3:51:59 PM"
    assert rows[0].elapsed_ms == 12
    assert rows[0].resolution_s == 1


@pytest.mark.parametrize("second_ms,values", [(43, (2,)), (44, (1,))])
def test_two_distinct_rows_cannot_be_silently_merged_at_one_second(second_ms, values):
    g, now = realtime_guard()
    text = "10/1/2026 3:51:59 PM"
    g.push((1,), "first", 43, source_text=text, wall=now, monotonic=100)
    with pytest.raises(MASsoftProtocolError, match="resolution"):
        g.push(values, "second", second_ms, source_text=text, wall=now, monotonic=100)
    assert g.snapshot()["queue_depth"] == 0


@pytest.mark.parametrize(
    "text,message",
    [
        ("10/1/2026 3:51:58 PM", "backwards"),
        ("10/1/2026 3:52:02 PM", "future"),
        ("00:00:02", "date/time"),
    ],
)
def test_bad_real_clock_latches_fault_and_discards_queue(text, message):
    g, now = realtime_guard()
    g.push((1,), "first", 43, source_text="10/1/2026 3:51:59 PM", wall=now, monotonic=100)
    with pytest.raises(MASsoftProtocolError, match=message):
        g.push((2,), "second", 44, source_text=text, wall=now, monotonic=100)
    with pytest.raises(MASsoftProtocolError, match=message):
        g.drain(wall=now, monotonic=100)


def test_identical_real_time_duplicate_is_ignored():
    g, now = realtime_guard()
    for _ in range(2):
        g.push((1,), "first", 43, source_text="10/1/2026 3:51:59 PM", wall=now, monotonic=100)
    assert g.duplicates == 1
    assert len(g.drain(wall=now, monotonic=100)) == 1


def test_extra_time_column_in_real_time_row_is_not_silently_consumed():
    with pytest.raises(MASsoftProtocolError):
        parse_realtime_row("10/1/2026 3:51:41 PM\t00:00:00\t43\t0\t1", expected_count=2)
