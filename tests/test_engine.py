"""Tests for the parts where being wrong is expensive.

Not a coverage exercise. These cover the interval-timestamp reconstruction
(silently wrong timestamps would corrupt every detector), idempotency (a
duplicate signal is a lie about how often something fired), the confidence
heuristic, and the YAML boolean trap that already broke the region code once.
"""

from __future__ import annotations

import json
import os
import shutil
import textwrap
from datetime import timedelta
from pathlib import Path

import pytest

os.environ.setdefault("SIGNAL_DB_PATH", "/tmp/sigarb-test.db")

from sigarb import db, peak, signals, sources, timeutil  # noqa: E402
from sigarb.adapters import csv_feed, xml_feed  # noqa: E402
from sigarb.detect import base as detect_base  # noqa: E402
from sigarb.detect.statistical import pct_change, zscore  # noqa: E402


# --------------------------------------------------------------------------
# Source configs
# --------------------------------------------------------------------------

def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "s.yaml"
    path.write_text(textwrap.dedent(body))
    return path


BASE_CONFIG = """
    source_id: t1
    name: Test
    region_code: "on"
    data_type: timeseries_numeric
    adapter: xml_feed
    update_frequency: 5m
    licence: test
    licence_scope: unknown
    terms_check: checked
    request:
      url: https://example.invalid/f.xml
"""


def test_region_code_bare_on_is_rejected(tmp_path):
    """YAML 1.1 reads bare `on` as True. Invariant 7 says that must fail loudly."""
    path = write_config(tmp_path, BASE_CONFIG.replace('region_code: "on"', "region_code: on"))
    with pytest.raises(sources.ConfigError, match="boolean"):
        sources.load_source(path)


def test_missing_terms_check_is_rejected(tmp_path):
    """Invariant 6: a source with no recorded terms check must not run."""
    path = write_config(tmp_path, BASE_CONFIG.replace("    terms_check: checked\n", ""))
    with pytest.raises(sources.ConfigError, match="terms_check"):
        sources.load_source(path)


def test_duration_parsing():
    assert sources.parse_duration("5m") == 300
    assert sources.parse_duration("1h") == 3600
    assert sources.parse_duration("2d") == 172800
    with pytest.raises(sources.ConfigError):
        sources.parse_duration("soon")


# --------------------------------------------------------------------------
# Interval timestamp reconstruction
# --------------------------------------------------------------------------

ZONAL_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="http://www.ieso.ca/schema">
<DocBody>
<DeliveryDate>2026-08-18</DeliveryDate>
<DeliveryHour>21</DeliveryHour>
<ZonalPrice><Interval>1</Interval><LmpCap>108.14</LmpCap><CongPriceCap>0.00</CongPriceCap></ZonalPrice>
<ZonalPrice><Interval>2</Interval><LmpCap>107.86</LmpCap><CongPriceCap>0.00</CongPriceCap></ZonalPrice>
<ZonalPrice><Interval>3</Interval><LmpCap/><CongPriceCap/></ZonalPrice>
</DocBody>
</Document>
"""


def zonal_source(tmp_path):
    path = write_config(tmp_path, """
        source_id: on_price
        name: Price
        region_code: "on"
        data_type: timeseries_numeric
        adapter: xml_feed
        update_frequency: 5m
        licence: test
        licence_scope: unknown
        terms_check: checked
        request:
          url: https://example.invalid/f.xml
        parse:
          mode: timeseries
          timezone: EST
          base_date_path: "d:DocBody/d:DeliveryDate"
          base_hour_path: "d:DocBody/d:DeliveryHour"
          hour_base: 1
          records_path: "d:DocBody/d:ZonalPrice"
          interval_path: "d:Interval"
          interval_minutes: 5
          interval_base: 1
          value_paths:
            price: "d:LmpCap"
            congestion: "d:CongPriceCap"
    """)
    return sources.load_source(path)


def test_hour_ending_interval_timestamps(tmp_path):
    """Hour 21 is hour-ending in EST, so interval 1 is 20:00 EST = 01:00 UTC."""
    result = xml_feed.parse(zonal_source(tmp_path), ZONAL_XML, payload_id=None)
    prices = {r["observed_at"]: r["value"] for r in result.observations if r["series_key"] == "price"}
    assert prices["2026-08-19T01:00:00Z"] == 108.14
    assert prices["2026-08-19T01:05:00Z"] == 107.86


def test_unsettled_intervals_are_skipped_not_zeroed(tmp_path):
    """Empty elements must never be read as zero, or every hour invents a crash."""
    result = xml_feed.parse(zonal_source(tmp_path), ZONAL_XML, payload_id=None)
    prices = [r for r in result.observations if r["series_key"] == "price"]
    assert len(prices) == 2
    assert all(r["value"] != 0 for r in prices)


TOTALS_CSV = b"""PM 20260818 \tNORMAL DISPATCH TOTALS;

RTEM_TOTALS;
\\\\CREATED AT 2026/08/18 20:47:27 FOR 2026/08/18
HOUR,INTERVAL,TOTAL ENERGY,TOTAL LOAD,FLAG
21,1,21986.7,21539.9,DSO-RD
21,2,21927.2,21482.5,DSO-RD
"""


def test_csv_date_from_preamble_and_interval(tmp_path):
    """The CSV carries no date column: it has to come out of the preamble line."""
    path = write_config(tmp_path, """
        source_id: on_totals
        name: Totals
        region_code: "on"
        data_type: timeseries_numeric
        adapter: csv_feed
        update_frequency: 5m
        licence: test
        licence_scope: unknown
        terms_check: checked
        request:
          url: https://example.invalid/f.csv
        parse:
          mode: timeseries
          timezone: EST
          skip_rows: 4
          date_from_preamble: 'FOR (\\d{4}/\\d{2}/\\d{2})'
          hour_column: HOUR
          interval_column: INTERVAL
          value_columns:
            load: "TOTAL LOAD"
    """)
    result = csv_feed.parse(sources.load_source(path), TOTALS_CSV, payload_id=None)
    got = {r["observed_at"]: r["value"] for r in result.observations}
    assert got == {"2026-08-19T01:00:00Z": 21539.9, "2026-08-19T01:05:00Z": 21482.5}


# --------------------------------------------------------------------------
# Detector maths
# --------------------------------------------------------------------------

def rows(values, start_hour=0):
    return [
        {"observed_at": timeutil.iso(
            __import__("datetime").datetime(2026, 8, 1, 0, 0)
            + __import__("datetime").timedelta(hours=start_hour + i)),
         "value": float(v)}
        for i, v in enumerate(values)
    ]


def noisy_baseline(n=40, spike=None):
    """A baseline needs some variance or a z-score is undefined by construction."""
    values = [100.0 + (1.0 if i % 2 else -1.0) for i in range(n)]
    if spike is not None:
        values.append(spike)
    return values


def test_zscore_baseline_excludes_the_point_being_judged():
    """A large move must not be allowed to inflate its own baseline."""
    found = zscore(rows(noisy_baseline(spike=500.0)),
                   {"window": 40, "min_periods": 20, "threshold": 3.0},
                   series_key="s", series_name="S")
    assert len(found) == 1
    assert found[0].details["baseline_mean"] == pytest.approx(100.0)
    assert found[0].details["baseline_n"] == 40
    assert found[0].direction == "up"


def test_zscore_is_silent_on_a_flat_series():
    found = zscore(rows(noisy_baseline(n=60)),
                   {"window": 40, "min_periods": 20, "threshold": 3.0},
                   series_key="s", series_name="S")
    assert found == []


def test_zero_variance_baseline_never_fires_and_never_divides_by_zero():
    """A perfectly constant series has an undefined z-score. Silence is correct."""
    found = zscore(rows([100.0] * 40 + [500.0]),
                   {"window": 40, "min_periods": 20, "threshold": 3.0},
                   series_key="s", series_name="S")
    assert found == []


def test_zscore_direction_filter():
    assert zscore(rows(noisy_baseline(spike=500.0)),
                  {"window": 40, "min_periods": 20, "threshold": 3.0,
                   "direction": "down"}, series_key="s", series_name="S") == []


def test_pct_change_threshold():
    found = pct_change(rows([100.0, 100.0, 100.0, 260.0]),
                       {"periods": 3, "threshold_pct": 150}, series_key="s", series_name="S")
    assert len(found) == 1
    assert found[0].magnitude == pytest.approx(160.0)


def test_confidence_is_half_at_the_threshold_and_one_at_double():
    assert detect_base.confidence(3.0, 3.0, 100, 100) == pytest.approx(0.5)
    assert detect_base.confidence(6.0, 3.0, 100, 100) == pytest.approx(1.0)
    # A thin baseline discounts the same breach.
    assert detect_base.confidence(6.0, 3.0, 25, 100) == pytest.approx(0.7)


def test_low_sample_flag():
    assert detect_base.sample_flags(10, 100) == ["low_sample"]
    assert detect_base.sample_flags(90, 100) == []


# --------------------------------------------------------------------------
# Signals: determinism, provenance, idempotency
# --------------------------------------------------------------------------

def test_signal_id_is_deterministic(tmp_path):
    source = zonal_source(tmp_path)
    detection = detect_base.Detection(
        detector="zscore", series_key="price", observed_at="2026-08-19T01:00:00Z",
        headline="h", plain_english="p", magnitude=4.25, confidence=0.8,
    )
    assert signals.signal_id(source, detection) == signals.signal_id(source, detection)


def test_every_signal_carries_provenance_and_disclaimers(tmp_path):
    import json

    source = zonal_source(tmp_path)
    row = signals.build_row(source, detect_base.Detection(
        detector="zscore", series_key="price", observed_at="2026-08-19T01:00:00Z",
        headline="h", plain_english="p", magnitude=4.25, confidence=0.8))
    flags = json.loads(row["disclaimer_json"])
    assert "source_aggregated" in flags and "not_financial_advice" in flags
    assert "model_derived" in flags
    # licence_scope unknown must surface the redistribution warning
    assert "redistribution_unresolved" in flags
    assert json.loads(row["provenance_json"])[0]["source_id"] == "on_price"


def test_inserts_are_idempotent(tmp_path):
    path = tmp_path / "t.db"
    with db.session(path) as conn:
        db.upsert_source(conn, {
            "source_id": "s", "name": "S", "region_code": "on",
            "data_type": "timeseries_numeric", "adapter": "rss",
            "update_frequency_s": 300, "licence": "l", "licence_scope": "unknown",
            "terms_check": "c", "signal_potential": "p", "enabled": 1, "config_json": "{}",
        })
        obs = [{"source_id": "s", "series_key": "k", "observed_at": "2026-08-19T01:00:00Z",
                "value": 1.0, "unit": None, "currency": None, "region_code": "on",
                "payload_id": None, "ingested_at": "2026-08-19T01:00:00Z"}]
        assert db.insert_observations(conn, obs) == 1
        assert db.insert_observations(conn, obs) == 0
        assert db.counts(conn)["observations"] == 1


# --------------------------------------------------------------------------
# Peak line: per-record dates and forecast horizons
# --------------------------------------------------------------------------

PEAK_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="http://www.ieso.ca/schema">
<DocHeader><CreatedAt>2026-08-19T11:10:35</CreatedAt></DocHeader>
<DocBody><xmldata>
<dataset datapointName="TOP_ONTARIO_DEMAND">
<datapoint><datetimeInfo><deliveryDate>2026-07-14</deliveryDate>
<deliveryHour>17</deliveryHour></datetimeInfo><value>25646.25</value></datapoint>
<datapoint><datetimeInfo><deliveryDate>2026-07-02</deliveryDate>
<deliveryHour>18</deliveryHour></datetimeInfo><value>24733.5</value></datapoint>
</dataset>
<dataset datapointName="TOP_AQEW_VALUES">
<datapoint><datetimeInfo><deliveryDate>2026-07-14</deliveryDate>
<deliveryHour>17</deliveryHour></datetimeInfo><value>24865.78</value></datapoint>
</dataset>
</xmldata></DocBody>
</Document>
"""


def peak_source(tmp_path: Path):
    path = write_config(tmp_path, """
        source_id: peak
        name: Peak tracker
        region_code: "on"
        data_type: timeseries_numeric
        adapter: xml_feed
        update_frequency: 1h
        licence: test
        licence_scope: unknown
        terms_check: checked
        request:
          url: https://example.invalid/f.xml
        parse:
          mode: timeseries
          timezone: EST
          records_path: ".//d:dataset[@datapointName='TOP_ONTARIO_DEMAND']/d:datapoint"
          date_path: "d:datetimeInfo/d:deliveryDate"
          hour_path: "d:datetimeInfo/d:deliveryHour"
          hour_base: 1
          value_path: "d:value"
          series_literal: top_ontario_demand
    """)
    return sources.load_source(path)


def test_records_carry_their_own_delivery_date(tmp_path):
    """Peak rows span a season, so each carries its own date rather than sharing one."""
    result = xml_feed.parse(peak_source(tmp_path), PEAK_XML, payload_id=None)
    stamps = {r["observed_at"]: r["value"] for r in result.observations}
    # Hour 17 is hour-ending in EST, so it starts at 16:00 EST = 21:00 UTC.
    assert stamps["2026-07-14T21:00:00Z"] == 25646.25
    assert stamps["2026-07-02T22:00:00Z"] == 24733.5


def test_the_aqew_datasets_are_not_blended_into_demand(tmp_path):
    """Six datasets share the file on a different measurement basis. Only one is the threshold."""
    result = xml_feed.parse(peak_source(tmp_path), PEAK_XML, payload_id=None)
    assert len(result.observations) == 2
    assert all(r["value"] > 24000 for r in result.observations)
    assert 24865.78 not in [r["value"] for r in result.observations]


FORECAST_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="http://www.ieso.ca/schema">
<DocHeader><CreatedAt>2026-08-19T08:00:00</CreatedAt></DocHeader>
<DocBody><DeliveryDate>2026-08-19</DeliveryDate>
<OntarioDemand><ForecastOntDemand>
<Demand><DeliveryHour>10</DeliveryHour><EnergyMW>19000</EnergyMW></Demand>
<Demand><DeliveryHour>18</DeliveryHour><EnergyMW>23000</EnergyMW></Demand>
<Demand><DeliveryHour>24</DeliveryHour><EnergyMW>20000</EnergyMW></Demand>
</ForecastOntDemand></OntarioDemand>
</DocBody>
</Document>
"""


def forecast_source(tmp_path: Path):
    path = write_config(tmp_path, """
        source_id: fc
        name: Demand forecast
        region_code: "on"
        data_type: timeseries_numeric
        adapter: xml_feed
        update_frequency: 1h
        licence: test
        licence_scope: unknown
        terms_check: checked
        request:
          url: https://example.invalid/f.xml
        parse:
          mode: timeseries
          timezone: EST
          base_date_path: "d:DocBody/d:DeliveryDate"
          records_path: ".//d:ForecastOntDemand/d:Demand"
          hour_path: "d:DeliveryHour"
          hour_base: 1
          value_path: "d:EnergyMW"
          series_literal: forecast_ontario_demand
          lead_time_path: "d:DocHeader/d:CreatedAt"
    """)
    return sources.load_source(path)


def test_forecast_readings_are_filed_by_horizon(tmp_path):
    """A 9am-for-5pm forecast and a 4pm-for-5pm revision are different claims."""
    result = xml_feed.parse(forecast_source(tmp_path), FORECAST_XML, payload_id=None)
    keys = {r["observed_at"]: r["series_key"] for r in result.observations}
    # Issued 08:00 EST. Hour 10 is hour-ending, so it starts 09:00: one hour out.
    assert keys["2026-08-19T14:00:00Z"] == "forecast_ontario_demand|lead0h"
    # Hour 18 starts 17:00, nine hours out.
    assert keys["2026-08-19T22:00:00Z"] == "forecast_ontario_demand|lead6h"
    # Hour 24 starts 23:00, fifteen hours out.
    assert keys["2026-08-20T04:00:00Z"] == "forecast_ontario_demand|lead12h"


def test_horizon_collapse_would_have_lost_revisions(tmp_path):
    """Without the horizon suffix every revision keys the same and the first wins."""
    spec = dict(forecast_source(tmp_path).parse)
    spec.pop("lead_time_path")
    source = forecast_source(tmp_path)
    object.__setattr__(source, "parse", spec)
    result = xml_feed.parse(source, FORECAST_XML, payload_id=None)
    assert {r["series_key"] for r in result.observations} == {"forecast_ontario_demand"}


def test_calendar_urls_resolve_in_market_time(tmp_path):
    """Resolving an EST-named file against UTC asks for tomorrow's file all evening."""
    from datetime import datetime, timezone as _tz
    path = write_config(tmp_path, """
        source_id: cal
        name: Calendar named
        region_code: "on"
        data_type: timeseries_numeric
        adapter: xml_feed
        update_frequency: 1h
        licence: test
        licence_scope: unknown
        terms_check: checked
        request:
          url: https://example.invalid/PUB_{date:%Y%m%d}.xml
          date_timezone: EST
    """)
    source = sources.load_source(path)
    # 01:00 UTC on the 20th is still 20:00 EST on the 19th.
    late = datetime(2026, 8, 20, 1, 0, tzinfo=_tz.utc)
    assert source.resolved_url(late).endswith("PUB_20260819.xml")


# --------------------------------------------------------------------------
# The peak call
# --------------------------------------------------------------------------

def _peak_db(tmp_path: Path, rows):
    path = tmp_path / "peak.db"
    conn_ctx = db.session(path)
    conn = conn_ctx.__enter__()
    for source_id in {r[0] for r in rows}:
        db.upsert_source(conn, {
            "source_id": source_id, "name": source_id, "region_code": "on",
            "data_type": "timeseries_numeric", "adapter": "xml_feed",
            "update_frequency_s": 3600, "licence": "l", "licence_scope": "unknown",
            "terms_check": "c", "signal_potential": "p", "enabled": 1, "config_json": "{}",
        })
    db.insert_observations(conn, [
        {"source_id": s, "series_key": k, "observed_at": ts, "value": v,
         "unit": "MW", "currency": None, "region_code": "on",
         "payload_id": None, "ingested_at": ts}
        for s, k, ts, v in rows
    ])
    return conn_ctx, conn


def test_base_period_runs_may_to_april():
    from datetime import datetime, timezone as _tz
    assert peak.base_period(datetime(2026, 8, 19, tzinfo=_tz.utc)).label == "May 2026 to April 2027"
    # 30 April still belongs to the period that opened the previous May.
    assert peak.base_period(datetime(2027, 4, 30, tzinfo=_tz.utc)).label == "May 2026 to April 2027"
    assert peak.base_period(datetime(2027, 5, 1, 12, tzinfo=_tz.utc)).label == "May 2027 to April 2028"


def test_top_hours_counts_one_hour_per_day(tmp_path):
    """Verified against IESO's own tracker: the ICI top five is one hour per day.

    The five highest single hours of the 2026 season were consecutive hours on
    one July evening. Ranking raw hours would fill the list with that one day and
    understate the threshold badly.
    """
    rows = [
        ("on_ieso_demand_actual", "ontario_demand", "2026-07-14T21:00:00Z", 25646.0),
        ("on_ieso_demand_actual", "ontario_demand", "2026-07-14T22:00:00Z", 25517.0),
        ("on_ieso_demand_actual", "ontario_demand", "2026-07-14T20:00:00Z", 25384.0),
        ("on_ieso_demand_actual", "ontario_demand", "2026-07-02T22:00:00Z", 24734.0),
        ("on_ieso_demand_actual", "ontario_demand", "2026-07-13T22:00:00Z", 23982.0),
    ]
    ctx, conn = _peak_db(tmp_path, rows)
    try:
        top = peak.top_hours(conn, peak.base_period(timeutil.parse("2026-08-19T00:00:00Z")))
        assert [round(v) for _, v in top] == [25646, 24734, 23982]
        assert len({peak._market_day(ts) for ts, _ in top}) == 3
    finally:
        ctx.__exit__(None, None, None)


def test_probability_is_withheld_until_it_is_calibrated():
    thin = peak.Horizon(bucket="|lead12h", lead_hours_low=12, error_sd=400.0, pairs=5, days=5)
    assert not thin.calibrated
    assert peak.probability_of_exceeding(23000.0, 23833.0, thin) is None


def test_pairs_crowded_into_a_few_days_do_not_count_as_calibration():
    """Hours inside one day miss together, so days bound the estimate, not pairs."""
    crowded = peak.Horizon(
        bucket="|lead24h", lead_hours_low=24, error_sd=400.0, error_bias=0.0,
        pairs=peak.MIN_PAIRS_TO_CALIBRATE * 3, days=3,
    )
    assert not crowded.calibrated
    assert "3 delivery day(s)" in crowded.shortfall
    assert peak.probability_of_exceeding(23800.0, 23833.0, crowded) is None


def test_probability_is_half_when_the_forecast_sits_on_the_threshold():
    horizon = peak.Horizon(
        bucket="|lead12h", lead_hours_low=12, error_sd=400.0, error_bias=0.0,
        pairs=peak.MIN_PAIRS_TO_CALIBRATE, days=peak.MIN_DAYS_TO_CALIBRATE,
    )
    assert peak.probability_of_exceeding(23833.0, 23833.0, horizon) == pytest.approx(0.5)
    # One standard deviation of headroom leaves the familiar 16 percent tail.
    assert peak.probability_of_exceeding(23433.0, 23833.0, horizon) == pytest.approx(0.1587, abs=1e-3)


def test_forecast_bias_moves_the_probability(tmp_path):
    """A forecast that historically runs low must not be read at face value."""
    horizon = peak.Horizon(
        bucket="|lead12h", lead_hours_low=12, error_sd=400.0, error_bias=300.0,
        pairs=peak.MIN_PAIRS_TO_CALIBRATE, days=peak.MIN_DAYS_TO_CALIBRATE,
    )
    unbiased = peak.Horizon(
        bucket="|lead12h", lead_hours_low=12, error_sd=400.0, error_bias=0.0,
        pairs=peak.MIN_PAIRS_TO_CALIBRATE, days=peak.MIN_DAYS_TO_CALIBRATE,
    )
    assert (peak.probability_of_exceeding(23600.0, 23833.0, horizon)
            > peak.probability_of_exceeding(23600.0, 23833.0, unbiased))


def test_the_freshest_forecast_wins_for_each_hour(tmp_path):
    """A narrower horizon band means the forecast was issued closer to the hour."""
    rows = [
        ("on_ieso_demand_forecast", "forecast_ontario_demand|lead24h", "2026-08-20T21:00:00Z", 22000.0),
        ("on_ieso_demand_forecast", "forecast_ontario_demand|lead6h", "2026-08-20T21:00:00Z", 24100.0),
        ("on_ieso_demand_forecast", "forecast_ontario_demand|leadpast", "2026-08-20T21:00:00Z", 19000.0),
    ]
    ctx, conn = _peak_db(tmp_path, rows)
    try:
        best = peak.latest_forecast_by_hour(conn, "2026-08-20T00:00:00Z")
        assert best["2026-08-20T21:00:00Z"] == (24100.0, "|lead6h")
    finally:
        ctx.__exit__(None, None, None)


def _call_db(tmp_path: Path, actual_rows=()):
    path = tmp_path / "calls.db"
    ctx = db.session(path)
    conn = ctx.__enter__()
    db.upsert_source(conn, {
        "source_id": peak.ACTUAL_SOURCE, "name": "actual", "region_code": "on",
        "data_type": "timeseries_numeric", "adapter": "csv_feed",
        "update_frequency_s": 3600, "licence": "l", "licence_scope": "unknown",
        "terms_check": "c", "signal_potential": "p", "enabled": 1, "config_json": "{}",
    })
    if actual_rows:
        db.insert_observations(conn, [
            {"source_id": peak.ACTUAL_SOURCE, "series_key": peak.ACTUAL_SERIES,
             "observed_at": ts, "value": v, "unit": "MW", "currency": None,
             "region_code": "on", "payload_id": None, "ingested_at": ts}
            for ts, v in actual_rows
        ])
    return ctx, conn


def _report(threshold: float, probability: float | None = 0.4):
    horizon = peak.Horizon(
        bucket="|lead12h", lead_hours_low=12, error_sd=400.0, error_bias=0.0,
        pairs=peak.MIN_PAIRS_TO_CALIBRATE, days=peak.MIN_DAYS_TO_CALIBRATE,
    )
    result = peak.PeakReport(period=peak.base_period(timeutil.parse("2026-08-19T00:00:00Z")))
    result.threshold = threshold
    result.calls = [peak.DayCall(
        date="2026-08-20", peak_hour_utc="2026-08-20T21:00:00Z",
        peak_hour_label="2026-08-20 HE17", forecast_mw=23600.0,
        margin_mw=23600.0 - threshold, horizon=horizon, probability=probability,
    )]
    return result


def test_a_call_is_recorded_once_per_horizon(tmp_path):
    """A later, easier call must never overwrite the one that was actually made."""
    ctx, conn = _call_db(tmp_path)
    try:
        assert peak.record_calls(conn, _report(23833.0)) == 1
        assert peak.record_calls(conn, _report(23833.0, probability=0.9)) == 0
        stored = conn.execute("SELECT probability FROM peak_calls").fetchall()
        assert [row[0] for row in stored] == [0.4]
    finally:
        ctx.__exit__(None, None, None)


def test_settling_grades_against_the_threshold_that_was_in_force(tmp_path):
    """The threshold moves as peaks land; grading against today's would misjudge."""
    # The market day runs 05:00Z to 05:00Z, not midnight to midnight.
    start, _ = peak.market_day_bounds("2026-08-20")
    base = timeutil.parse(start)
    day = [(timeutil.iso(base + timedelta(hours=hour)), 20000.0) for hour in range(24)]
    day[16] = (timeutil.iso(base + timedelta(hours=16)), 24000.0)   # HE17, above 23833
    ctx, conn = _call_db(tmp_path, day)
    try:
        peak.record_calls(conn, _report(23833.0))
        # A higher threshold arrives later. The stored call keeps the old one.
        conn.execute("UPDATE peak_calls SET threshold_mw = threshold_mw")
        assert peak.settle_calls(conn, now=timeutil.parse("2026-08-22T12:00:00Z")) == 1
        row = conn.execute("SELECT actual_mw, cleared FROM peak_calls").fetchone()
        assert row[0] == 24000.0
        assert row[1] == 1
    finally:
        ctx.__exit__(None, None, None)


def test_a_partial_day_is_left_open_rather_than_graded(tmp_path):
    """Half a day of actuals must not settle a call against an afternoon that is still running."""
    start, _ = peak.market_day_bounds("2026-08-20")
    base = timeutil.parse(start)
    partial = [(timeutil.iso(base + timedelta(hours=hour)), 20000.0) for hour in range(10)]
    ctx, conn = _call_db(tmp_path, partial)
    try:
        peak.record_calls(conn, _report(23833.0))
        assert peak.settle_calls(conn, now=timeutil.parse("2026-08-22T12:00:00Z")) == 0
        assert conn.execute("SELECT settled_at FROM peak_calls").fetchone()[0] is None
    finally:
        ctx.__exit__(None, None, None)


def test_the_scoreboard_refuses_to_praise_a_model_worse_than_the_base_rate(tmp_path):
    ctx, conn = _call_db(tmp_path)
    try:
        rows = []
        for index in range(30):
            cleared = 1 if index < 6 else 0          # a 20 percent base rate
            rows.append(("on", f"2026-07-{index + 1:02d}", "|lead12h", "2026-07-01T00:00:00Z",
                         23000.0, 23100.0, "2026-07-01T21:00:00Z",
                         0.9 if cleared == 0 else 0.1,   # confidently backwards
                         50, 20, 23200.0, cleared, "2026-08-01T00:00:00Z"))
        conn.executemany(
            "INSERT INTO peak_calls (region_code, delivery_date, horizon_bucket, made_at,"
            " threshold_mw, forecast_mw, peak_hour_utc, probability, pairs, days,"
            " actual_mw, cleared, settled_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        board = peak.scoreboard(conn)
        assert board["scored"] == 30
        assert board["brier"] > board["base_rate"] * (1 - board["base_rate"])
        assert "should not be sold" in board["verdict"]
    finally:
        ctx.__exit__(None, None, None)


def test_the_agent_runs_at_load_and_points_at_this_checkout():
    """A schedule that looks installed but does nothing is the worst outcome."""
    from sigarb import schedule as schedule_mod

    definition = schedule_mod._definition(300)
    assert definition["RunAtLoad"] is True
    assert definition["StartInterval"] == 300
    script = Path(definition["ProgramArguments"][-1])
    assert script.is_file(), "the plist points at a script that is not there"
    assert script.read_text().rstrip().endswith("exit 0"), (
        "the cycle must exit zero, or launchd throttles the agent and then stops it"
    )


def test_a_quiet_day_raises_nothing():
    result = _report(23833.0, probability=0.02)
    assert peak.worth_raising(result) is None


def test_a_likely_peak_is_worth_interrupting_someone_for():
    result = _report(23833.0, probability=0.62)
    headline = peak.worth_raising(result)
    assert headline is not None and headline.startswith("Curtail")
    assert "62%" in headline


def test_an_uncalibrated_call_never_raises_an_alert():
    """An unknown chance is not a quiet day, but it is not a claim either."""
    assert peak.worth_raising(_report(23833.0, probability=None)) is None


def test_the_scheduled_cycle_gates_its_daily_steps_on_a_marker(tmp_path):
    """The cycle fires every few minutes; the daily steps must not.

    Without the marker, an hour-range test alone runs the peak call on every
    cycle in the window, which is roughly sixty notification attempts a morning.
    """
    from sigarb import schedule as schedule_mod

    script = Path(schedule_mod._definition(300)["ProgramArguments"][-1]).read_text()
    assert "once_today" in script
    for step in ("peak-call", "backup"):
        assert f"once_today {step}" in script, f"the {step} step is not gated"
    # Market time, not local: the call is keyed on the EST market day.
    assert "TZ=EST date" in script
    assert "$(date +%H)" not in script, "local hour would straddle the market boundary"


def test_one_overnight_day_does_not_stretch_the_peak_window(tmp_path):
    """A quiet day can peak at any hour; only contending days define the window.

    The first version spanned the lowest to the highest peak hour seen, which one
    3am winter day widened to almost the whole clock, and calibrating on almost
    every hour quotes a confidence the forecast does not have.
    """
    rows = []
    # Ten contending summer days, all peaking hour-ending 18 (17:00 EST).
    for day in range(1, 11):
        for hour in range(24):
            rows.append((f"2026-07-{day:02d}", hour, 20000.0 + (600.0 if hour == 17 else 0.0)))
    # One quiet day whose own maximum lands overnight.
    for hour in range(24):
        rows.append(("2026-11-05", hour, 15000.0 + (100.0 if hour == 3 else 0.0)))

    observations = []
    for date, hour, value in rows:
        start, _ = peak.market_day_bounds(date)
        observations.append((timeutil.iso(timeutil.parse(start) + timedelta(hours=hour)), value))

    ctx, conn = _call_db(tmp_path, observations)
    try:
        window = peak.peak_hour_window(conn, peak.base_period(timeutil.parse("2026-11-20T00:00:00Z")))
        assert window == {16, 17, 18}, window
        assert 3 not in window
    finally:
        ctx.__exit__(None, None, None)


def test_a_probability_is_never_zero_or_one():
    """The recorded call must not claim a certainty the CLI refuses to print.

    `signal peak` shows "under 0.1%" rather than 0%, and the stored number has to
    agree with it: the record is the artefact a buyer would be shown, and a
    normal approximation is not entitled to assert impossibility.
    """
    horizon = peak.Horizon(
        bucket="|lead6h", lead_hours_low=6, error_sd=400.0, error_bias=0.0,
        pairs=peak.MIN_PAIRS_TO_CALIBRATE, days=peak.MIN_DAYS_TO_CALIBRATE,
    )
    hopeless = peak.probability_of_exceeding(15000.0, 23833.0, horizon)
    certain = peak.probability_of_exceeding(30000.0, 23833.0, horizon)
    assert hopeless == peak.PROBABILITY_FLOOR
    assert certain == 1.0 - peak.PROBABILITY_FLOOR


# --------------------------------------------------------------------------
# The Worker port
# --------------------------------------------------------------------------

def test_the_worker_constants_match_the_python_ones():
    """Two implementations of one calculation drift unless something checks.

    The peak line runs on Cloudflare and the maths is ported to JavaScript. These
    numbers decide what the engine is willing to claim, so a value changed on one
    side only is a silent disagreement about the product.
    """
    import re
    import yaml

    root = Path(__file__).resolve().parent.parent
    source = root / "worker" / "src" / "constants.js"
    assert source.is_file(), "the Worker constants file is missing"
    text = source.read_text()

    def js(name):
        match = re.search(rf"^export const {name} = (.+?);(?:\s*//.*)?$", text, re.M)
        assert match, f"{name} is not exported from constants.js"
        return match.group(1).strip()

    for name, expected in (
        ("MIN_PAIRS_TO_CALIBRATE", peak.MIN_PAIRS_TO_CALIBRATE),
        ("MIN_DAYS_TO_CALIBRATE", peak.MIN_DAYS_TO_CALIBRATE),
        ("PROBABILITY_FLOOR", peak.PROBABILITY_FLOOR),
        ("PEAK_RANK", peak.PEAK_RANK),
        ("BASE_PERIOD_FIRST_MONTH", peak.BASE_PERIOD_FIRST_MONTH),
        ("PEAK_WINDOW_MARGIN_HOURS", peak.PEAK_WINDOW_MARGIN_HOURS),
        ("PEAK_WINDOW_CONTENDER_DAYS", peak.PEAK_WINDOW_CONTENDER_DAYS),
        ("PEAK_WINDOW_CONTENDER_FLOOR", peak.PEAK_WINDOW_CONTENDER_FLOOR),
        ("NOTIFY_ABOVE", peak.NOTIFY_ABOVE),
    ):
        assert float(js(name)) == expected, name

    assert js("ACTUAL_SOURCE").strip('"') == peak.ACTUAL_SOURCE
    assert js("ACTUAL_SERIES").strip('"') == peak.ACTUAL_SERIES
    assert js("TRACKER_SOURCE").strip('"') == peak.TRACKER_SOURCE
    assert json.loads(js("FORECAST_SOURCES")) == list(peak.FORECAST_SOURCES)
    # EST is fixed at UTC-5 year round, which is why the Worker hardcodes it.
    assert float(js("MARKET_UTC_OFFSET_HOURS")) == -5

    buckets = json.loads(js("LEAD_TIME_BUCKETS"))
    for name in peak.FORECAST_SOURCES:
        config = yaml.safe_load((root / "sources" / f"{name}.yaml").read_text())
        assert config["parse"]["lead_time_buckets"] == buckets, name


def test_a_pulled_call_never_overwrites_one_already_here(tmp_path):
    """A call is immutable once made, so a conflict is a disagreement to look at.

    Settlement is the one field allowed to arrive later, and only onto a call
    that does not have one.
    """
    from sigarb import remote

    ctx, conn = _call_db(tmp_path)
    try:
        peak.record_calls(conn, _report(23833.0, probability=0.4))
        remote._merge_calls(conn, [{
            "region_code": "on", "delivery_date": "2026-08-20", "horizon_bucket": "|lead12h",
            "made_at": "2026-08-19T23:00:00Z", "threshold_mw": 23833.0,
            "forecast_mw": 99999.0, "peak_hour_utc": "2026-08-20T21:00:00Z",
            "probability": 0.99, "pairs": 1, "days": 1,
            "actual_mw": 24000.0, "cleared": 1, "settled_at": "2026-08-22T00:00:00Z",
        }])
        row = conn.execute(
            "SELECT probability, forecast_mw, actual_mw, cleared FROM peak_calls"
        ).fetchone()
        assert row[0] == 0.4, "the local claim was overwritten"
        assert row[1] == 23600.0
        # Settlement is new information about a call already made, so it lands.
        assert row[2] == 24000.0 and row[3] == 1
    finally:
        ctx.__exit__(None, None, None)


def test_the_local_peak_command_does_not_record_by_default():
    """Two stores recording the same call would make the first-call rule a race."""
    source = (Path(__file__).resolve().parent.parent / "sigarb" / "cli.py").read_text()
    assert 'record = bool(getattr(args, "record", False))' in source
    assert "peak.record_calls(conn, result) if record else 0" in source


def test_the_worker_maths_passes_its_own_tests():
    """The ported calculation is checked by node, run from here so one gate covers both.

    The constants test above catches a number changed on one side. This catches
    behaviour that drifted without any constant moving, which is the more likely
    way two implementations of one calculation come apart.
    """
    import subprocess

    root = Path(__file__).resolve().parent.parent
    if shutil.which("node") is None:
        pytest.skip("node is not installed, so the Worker tests cannot run here")
    result = subprocess.run(
        ["node", "worker/test.mjs"], cwd=root, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout


# --------------------------------------------------------------------------
# The relay: Cloudflare collects, this machine detects
# --------------------------------------------------------------------------

def _relayed_source_ids() -> list[str]:
    """The source ids the Worker relays, read from its own source table."""
    import re

    root = Path(__file__).resolve().parent.parent
    text = (root / "worker" / "src" / "ingest.js").read_text()
    constants = (root / "worker" / "src" / "constants.js").read_text()
    named = dict(re.findall(r'^export const (\w+) = "([^"]+)";', constants, re.M))
    lists = dict(re.findall(r"^export const (\w+) = (\[[^\]]*\]);", constants, re.M))

    ids = []
    for token in re.findall(r"^\s*id: ([^,]+),", text, re.M):
        token = token.strip()
        if token in named:
            ids.append(named[token])
        else:
            match = re.fullmatch(r"(\w+)\[(\d+)\]", token)
            assert match and match.group(1) in lists, f"unresolved source id {token}"
            ids.append(json.loads(lists[match.group(1)])[int(match.group(2))])
    return ids


def test_every_source_the_worker_relays_has_a_config_here():
    """The Worker relays bytes; this machine is what knows how to read them.

    An id on the Worker with no matching file in `sources/` means payloads pile
    up in R2 and `signal rebuild` silently skips every one of them, because
    `rebuild_from_raw` maps a blob to a parser by its directory name.
    """
    root = Path(__file__).resolve().parent.parent
    ids = _relayed_source_ids()
    assert len(ids) >= 7, "the Worker's source table did not parse"
    for source_id in ids:
        assert (root / "sources" / f"{source_id}.yaml").is_file(), source_id


def test_a_relayed_object_key_is_the_local_filename(tmp_path):
    """The seam the whole sync rests on, checked across both languages.

    The Worker names an object `raw/<source>/<day>/<stamp>-<hash12>.<ext>` and
    this machine writes `<stamp>-<hash12>.<ext>.gz`. If those two ever disagree,
    a pull re-downloads everything forever and `signal pull` cannot resume from
    its own newest file.
    """
    import subprocess

    from sigarb import fetch, remote

    if shutil.which("node") is None:
        pytest.skip("node is not installed")

    root = Path(__file__).resolve().parent.parent
    body = b"<xml>a payload</xml>"
    fetched_at = "2026-08-23T16:02:17Z"

    script = (
        'import { objectKey } from "./worker/src/relay.js";'
        'import { createHash } from "node:crypto";'
        f'const hash = createHash("sha256").update({json.dumps(body.decode())}).digest("hex");'
        f'console.log(objectKey("on_ieso_zonal_price", new Date("{fetched_at}"), hash, "xml"));'
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    key = result.stdout.strip()

    os.environ["SIGNAL_RAW_DIR"] = str(tmp_path)
    try:
        path, _, _ = fetch.store_raw("on_ieso_zonal_price", body, "text/xml", fetched_at)
        assert path.name == f"{key.rsplit('/', 1)[-1]}.gz"
        assert remote.newest_local_key("on_ieso_zonal_price") == key
        assert remote._already_here(key) is True
        assert remote._fetched_at(key) == fetched_at
    finally:
        os.environ.pop("SIGNAL_RAW_DIR", None)


def test_a_payload_whose_hash_does_not_match_its_key_is_refused(tmp_path, monkeypatch):
    """Raw is what everything else is derived from, so it takes nothing on trust.

    The key carries the first twelve characters of the payload's SHA-256, which
    makes every download check itself for free.
    """
    from sigarb import remote

    class Answer:
        content = b"not the payload the key describes"

    monkeypatch.setattr(remote, "_authed", lambda *a, **k: Answer())
    monkeypatch.setenv("SIGNAL_RAW_DIR", str(tmp_path))

    key = "raw/on_ieso_zonal_price/20260823/20260823T160217Z-abcdef012345.xml"
    size, problem = remote._download({"key": key})
    assert size == 0
    assert "content hash" in problem
    assert not list(tmp_path.rglob("*.gz")), "a payload that failed its check was written"


def test_the_pull_resumes_from_its_own_newest_file(tmp_path, monkeypatch):
    """No cursor file: delete data/raw and the next pull refills it.

    The resume point is derived from what is on disk, so the two cannot drift.
    """
    from sigarb import remote

    monkeypatch.setenv("SIGNAL_RAW_DIR", str(tmp_path))
    assert remote.newest_local_key("on_ieso_zonal_price") is None

    directory = tmp_path / "on_ieso_zonal_price" / "20260823"
    directory.mkdir(parents=True)
    for name in ("20260823T160000Z-aaaaaaaaaaaa.xml.gz", "20260823T160500Z-bbbbbbbbbbbb.xml.gz"):
        (directory / name).write_bytes(b"")
    # A later day, to prove the ordering is not just within one directory.
    later = tmp_path / "on_ieso_zonal_price" / "20260901"
    later.mkdir(parents=True)
    (later / "20260901T000000Z-cccccccccccc.xml.gz").write_bytes(b"")

    assert remote.newest_local_key("on_ieso_zonal_price") == (
        "raw/on_ieso_zonal_price/20260901/20260901T000000Z-cccccccccccc.xml"
    )
