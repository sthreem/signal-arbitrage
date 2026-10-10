/**
 * Shared with sigarb/peak.py and sigarb/config.py.
 *
 * These numbers decide what the engine is willing to claim, so the two
 * implementations must not drift. `tests/test_engine.py` reads this file and
 * asserts every value matches its Python counterpart, and fails the build if
 * one is changed on only one side.
 */

// IESO stamps EST year round, not local Toronto time, so this is a constant.
export const MARKET_UTC_OFFSET_HOURS = -5;

export const BASE_PERIOD_FIRST_MONTH = 5;   // the ICI base period runs 1 May to 30 April
export const PEAK_RANK = 5;                 // charges are set by the top five

export const MIN_PAIRS_TO_CALIBRATE = 30;
export const MIN_DAYS_TO_CALIBRATE = 10;
export const PROBABILITY_FLOOR = 0.001;

export const PEAK_WINDOW_MARGIN_HOURS = 1;
export const PEAK_WINDOW_CONTENDER_DAYS = 20;
export const PEAK_WINDOW_CONTENDER_FLOOR = 0.85;

export const NOTIFY_ABOVE = 0.15;

export const LEAD_TIME_BUCKETS = [0, 3, 6, 12, 24, 48, 120, 336];

export const ACTUAL_SOURCE = "on_ieso_demand_actual";
export const ACTUAL_SERIES = "ontario_demand";
export const TRACKER_SOURCE = "on_ieso_ici_peak_tracker";
export const FORECAST_SOURCES = ["on_ieso_demand_forecast", "on_ieso_demand_forecast_ahead"];
export const FORECAST_SERIES_STEM = "forecast_ontario_demand";
export const REGION_CODE = "on";

// The market signal feeds. Relayed to R2 and never parsed here: their detectors
// need scikit-learn over a long history, so Cloudflare collects and the Mac
// detects. Every id must name a file in sources/, since that config is what the
// Mac uses to parse the payload back. A test asserts it.
export const ZONAL_SOURCE = "on_ieso_zonal_price";
export const TOTALS_SOURCE = "on_ieso_realtime_totals";
export const GENERATOR_SOURCE = "on_ieso_generator_output";

// Where things sit in the bucket.
export const RAW_PREFIX = "raw/";
export const STATE_PREFIX = "state/";

// R2 is a relay, not an archive. Long enough that a Mac left off for a month
// loses nothing, short enough that roughly 24 MB a day cannot reach the free
// tier's 10 GB.
export const RELAY_RETENTION_DAYS = 45;
