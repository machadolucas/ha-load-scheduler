# Changelog

## 0.20.0 — 2026-11-17

- Scattered (non-sequential) loads now run in whole quarter-hours. Hourly price
  slots are cut into quarters, the need is rounded up, and a need under 5 minutes
  buys nothing. Nothing is trimmed to the second any more: a fractional
  min-service floor split one run into 23:15–23:29:50 and 23:30–00:00, a hole
  the relay clicked through. Runs no longer last seconds or a few minutes.
- A stopped load only starts in a quarter already under way while at least
  10 minutes of it remain. A running load with a runtime target finishes its
  quarter once the remaining need drops under 5 minutes.
- A price entity that is briefly unavailable no longer drops loads to their
  failsafe start. The last good prices are used for up to 15 minutes while they
  still cover now (an 8-second failsafe run at 00:08 was the trigger).
- At the end of a planned period the plan is refreshed before the load is
  switched off, and the 5-minute safety-net replan is pinned to the clock just
  after each 5-minute mark. Together these remove the ~40-second off/on clicks
  between back-to-back runs.
- The cards round period times to the nearest minute instead of truncating.

## 0.18.2 — 2026-11-11

- Declare optional HTTP, Lovelace and recorder dependencies used by the integration.
- Fix the Boost action target schema while keeping its integration entity filter.
- Enable GitHub Actions tests, lint and Home Assistant/HACS validation.
- Keep scheduling logic and existing configuration unchanged.

## 0.18.1 — 2026-11-11

- Add refreshed integration artwork as bundled 256 px and 512 px icons for
  Home Assistant 2026.3 and later.
- Keep the high-resolution artwork master in `brands/icon-master.png` and
  retain the previous SVG/PNG assets as legacy references.
- Correct the branding guidance: local icons do not require an upstream
  `home-assistant/brands` PR. HACS catalog artwork is a separate concern.
- Keep the existing Home Assistant minimum and runtime behavior unchanged.

For earlier releases, see the [GitHub release history](https://github.com/machadolucas/ha-load-scheduler/releases).
