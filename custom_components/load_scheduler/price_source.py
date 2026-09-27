"""Normalise heterogeneous price-forecast entities into a common slot list.

Different price integrations expose their forecast completely differently
(Nord Pool, ENTSO-e, the user's ``nordpool_fi_day_ahead`` template, …). This
module auto-detects the shape and returns a uniform list of :class:`ForecastSlot`
(tz-aware ``start``/``end`` + ``buy`` and optional ``sell``), which the
coordinator then turns into :class:`engine.Slot` (adding solar excess).

Design choice mirroring the engine: the parsing core is **pure** and operates on
a plain attributes ``dict`` (no Home Assistant import), so it is unit-testable in
isolation. The thin ``slots_from_state`` wrapper adapts an HA ``State``.

Supported attribute layouts (auto-detected, in order):

* combined buy+sell items under ``data_today`` / ``data_tomorrow``
  (the user's day-ahead template: ``{start, end, buy, sell}``);
* split today/tomorrow lists: ``raw_today``/``raw_tomorrow`` (Nord Pool),
  ``prices_today``/``prices_tomorrow``, ``today_interval_prices``/…;
* a single list under ``prices`` / ``data`` / ``forecast`` (ENTSO-e etc.).

A matching ``*_yesterday`` list is read too when present. Feeds anchored to the
*market* day rather than the local one (Nord Pool's delivery day is CET/CEST, so
in Helsinki it starts at 01:00) keep the slots covering the first local hour of
the day in that list; ignoring it left the scheduler blind from 00:00 to 01:00.

Per-item keys are detected from a small candidate set; ``start`` may be an ISO
string or a ``datetime``; ``end`` is taken from the item, else the next item's
start, else inferred from the dominant slot length.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Attribute name triples: the *previous* day's list, the *today* list and the
# matching *tomorrow* list. The previous-day list matters because a feed can be
# anchored to a market day rather than the local one: Nord Pool's delivery day is
# CET/CEST-based, which in Helsinki starts at 01:00, so between local midnight
# and 01:00 the slots covering *now* still live in yesterday's list.
_SPLIT_ATTR_TRIPLES: list[tuple[str, str, str]] = [
    ("data_yesterday", "data_today", "data_tomorrow"),
    ("raw_yesterday", "raw_today", "raw_tomorrow"),
    ("prices_yesterday", "prices_today", "prices_tomorrow"),
    ("yesterday_interval_prices", "today_interval_prices", "tomorrow_interval_prices"),
]
# Attribute names that hold a single combined list (today + tomorrow together).
_SINGLE_ATTRS: list[str] = ["prices", "data", "forecast"]

_START_KEYS = ["start", "time", "hour", "start_time", "datetime", "period_start"]
_END_KEYS = ["end", "end_time"]
_BUY_KEYS = ["buy", "value", "price", "price_ct_per_kwh", "electricity_price"]
_SELL_KEYS = ["sell", "sell_price"]


@dataclass(frozen=True)
class ForecastSlot:
    """One normalised price slot. ``sell`` is ``None`` when not provided."""

    start: datetime
    end: datetime
    buy: float
    sell: float | None = None


@dataclass(frozen=True)
class FormatSpec:
    """Detected layout of a price entity's attributes."""

    today_attr: str
    tomorrow_attr: str | None
    start_key: str
    buy_key: str
    sell_key: str | None
    end_key: str | None
    yesterday_attr: str | None = None


class PriceFormatError(ValueError):
    """Raised when a price entity's attributes can't be understood."""


def _first_present(keys: list[str], item: dict) -> str | None:
    return next((k for k in keys if k in item), None)


def detect_format(attributes: dict) -> FormatSpec:
    """Work out where the forecast lives and which keys to read.

    Raises :class:`PriceFormatError` if no known layout matches.
    """
    today_attr: str | None = None
    tomorrow_attr: str | None = None
    yesterday_attr: str | None = None

    for yesterday, today, tomorrow in _SPLIT_ATTR_TRIPLES:
        if today in attributes:
            today_attr, tomorrow_attr = today, tomorrow
            yesterday_attr = yesterday if yesterday in attributes else None
            break
    if today_attr is None:
        today_attr = next((a for a in _SINGLE_ATTRS if a in attributes), None)
        tomorrow_attr = None

    if today_attr is None:
        raise PriceFormatError("no recognised forecast attribute found")

    # Key detection reads the first *non-empty* list: a market-day-anchored feed
    # can legitimately have an empty ``data_today`` right after its rollover
    # while yesterday's (or tomorrow's) list still describes the format.
    sample: list = []
    for attr in (yesterday_attr, today_attr, tomorrow_attr):
        candidate = attributes.get(attr) if attr else None
        if isinstance(candidate, list) and candidate:
            sample = candidate
            break
    if not sample or not isinstance(sample[0], dict):
        raise PriceFormatError(f"attribute {today_attr!r} is not a list of dicts")

    item = sample[0]
    start_key = _first_present(_START_KEYS, item)
    buy_key = _first_present(_BUY_KEYS, item)
    if start_key is None or buy_key is None:
        raise PriceFormatError("could not find start/value keys in forecast items")

    return FormatSpec(
        today_attr=today_attr,
        tomorrow_attr=tomorrow_attr,
        start_key=start_key,
        buy_key=buy_key,
        sell_key=_first_present(_SELL_KEYS, item),
        end_key=_first_present(_END_KEYS, item),
        yesterday_attr=yesterday_attr,
    )


def _parse_dt(value: object) -> datetime:
    """Parse an ISO string / pass through a ``datetime``, normalised to **UTC**.

    Normalising to UTC here is what keeps the engine DST-correct: all downstream
    arithmetic (``+timedelta``, subtraction) then runs in a zone without DST, so
    a slot that straddles a transition is still its true real-time length.
    Display/actuation converts back to local at the entity layer.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        raise PriceFormatError(f"unparseable start value: {value!r}")
    if dt.tzinfo is None:
        raise PriceFormatError(f"start time is not timezone-aware: {value!r}")
    return dt.astimezone(UTC)


def _infer_slot_length(starts: list[datetime]) -> timedelta:
    """Most common gap between consecutive starts (fallback 15 min)."""
    gaps: dict[float, int] = {}
    for a, b in zip(starts, starts[1:], strict=False):
        secs = (b - a).total_seconds()
        if secs > 0:
            gaps[secs] = gaps.get(secs, 0) + 1
    if not gaps:
        return timedelta(minutes=15)
    return timedelta(seconds=max(gaps, key=lambda s: gaps[s]))


# Buy keys whose values are in cents; the engine works in €/kWh, so a feed that
# says "ct" in its key is scaled rather than read 100× too expensive.
_CENT_KEYS = {"price_ct_per_kwh"}


def _to_float(value: object) -> float | None:
    """A price as a float, or ``None`` for a missing/unparseable value."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_list(items: list, spec: FormatSpec) -> list[ForecastSlot]:
    """Turn a raw item list into time-ordered ForecastSlots (end inferred if absent).

    Malformed items are skipped rather than failing the whole forecast: Nord
    Pool publishes ``raw_tomorrow`` with ``null`` values before the auction, and
    one bad item must not blind every load. Items are sorted and de-duplicated
    by start *before* a missing end is inferred from the next start, so an
    unordered feed or an overlapping list tail can't fabricate a negative or
    zero-length slot. Ends are inferred from every item with a usable *start*,
    priced or not, so a null price leaves a gap instead of stretching the
    previous (possibly cheap) slot across it.
    """
    scale = 0.01 if spec.buy_key in _CENT_KEYS else 1.0
    parsed: list[tuple[datetime, datetime | None, float | None, float | None]] = []
    seen: set[datetime] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            start = _parse_dt(it.get(spec.start_key))
            end = (
                _parse_dt(it[spec.end_key])
                if spec.end_key and it.get(spec.end_key) is not None
                else None
            )
        except (PriceFormatError, ValueError):
            continue
        if start in seen:
            continue  # keep the first copy, matching list order
        seen.add(start)
        buy = _to_float(it.get(spec.buy_key))
        sell = _to_float(it.get(spec.sell_key)) if spec.sell_key else None
        parsed.append((start, end, None if buy is None else buy * scale, sell))
    parsed.sort(key=lambda p: p[0])

    slot_len = _infer_slot_length([p[0] for p in parsed])
    slots: list[ForecastSlot] = []
    for i, (start, end, buy, sell) in enumerate(parsed):
        if buy is None:
            continue
        if end is None or end <= start:
            end = parsed[i + 1][0] if i + 1 < len(parsed) else start + slot_len
        slots.append(ForecastSlot(start=start, end=end, buy=buy, sell=sell))
    return slots


def normalize(attributes: dict, spec: FormatSpec | None = None) -> list[ForecastSlot]:
    """Normalise a price entity's attributes into time-ordered ForecastSlots.

    Concatenates the yesterday, today and tomorrow lists (either flank may be
    missing) and parses them; ``_parse_list`` sorts by start and drops
    exact-duplicate start times (keeping the first, in yesterday → tomorrow
    order), which guards against overlapping list tails.
    Yesterday's slots are kept rather than filtered here because the engine's
    window already discards anything that has fully elapsed — and dropping them
    eagerly is exactly what made a market-day-anchored feed invisible for the
    hour between local midnight and the market day's start.
    """
    spec = spec or detect_format(attributes)
    seen_attrs: list[str] = []
    for attr in (spec.yesterday_attr, spec.today_attr, spec.tomorrow_attr):
        if attr and attr not in seen_attrs:
            seen_attrs.append(attr)
    raw: list[dict] = []
    for attr in seen_attrs:
        raw += list(attributes.get(attr) or [])

    slots = _parse_list(raw, spec)
    if raw and not slots:
        raise PriceFormatError("no usable items in the price forecast")
    return slots


def merge_sell(buy_slots: list[ForecastSlot], sell_slots: list[ForecastSlot]) -> list[ForecastSlot]:
    """Attach sell prices from a *separate* sell-forecast entity.

    Used when buy and sell come from two different entities; the sell entity is
    normalised the same way (its ``buy`` field carries the sell value). A buy
    slot takes the sell slot whose interval *contains* its start, not just one
    with an identical start: an hourly sell feed against a quarter-hourly buy
    feed would otherwise leave three of every four slots without a sell price.
    Nothing is extrapolated beyond the sell feed's coverage.
    """
    ordered = sorted(sell_slots, key=lambda s: s.start)
    starts = [s.start for s in ordered]

    def sell_at(when: datetime) -> float | None:
        i = bisect_right(starts, when) - 1
        if i >= 0 and ordered[i].start <= when < ordered[i].end:
            return ordered[i].buy
        return None

    out: list[ForecastSlot] = []
    for b in buy_slots:
        sell = sell_at(b.start)
        out.append(
            ForecastSlot(start=b.start, end=b.end, buy=b.buy, sell=b.sell if sell is None else sell)
        )
    return out


def slots_from_state(state) -> list[ForecastSlot]:
    """Normalise a Home Assistant ``State`` (duck-typed: anything with
    ``.attributes``). Kept here, not in the coordinator, so the only HA-aware
    surface is this one-liner and the rest stays unit-testable.
    """
    if state is None:
        raise PriceFormatError("price entity is unavailable (no state)")
    return normalize(dict(state.attributes))
