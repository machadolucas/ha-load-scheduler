# Changelog

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
