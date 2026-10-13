# Changelog

## 0.9.1 (fork)
### Added
- Showcase dashboard (docs/examples/dashboard-showcase.yaml), screenshots and
  a check against a real bill in the README.

## 0.9.0 (fork)
### Added
- Power peaks: "Last day power peak" and "Month power peak" sensors (15-minute
  average, with time, band and share of the available power), a "Contract
  power" sensor read from E-Distribuzione, and an hourly mean/min/max power
  statistic per POD.

## 0.8.0 (fork)
### Added
- Energy costs: set your offer (single price or per F1/F2/F3 band, other
  per-kWh costs, fixed and power fees, VAT) in the options. The integration
  writes a EUR cost statistic per POD, links it in the Energy Dashboard and
  adds the "Last day cost" and "Month spending" sensors.

## 0.7.1 (fork)
### Changed
- Sign-in happens in your own browser: the setup shows an OAuth link and you
  paste back the `eneldist://redirect` address. Home Assistant no longer
  receives email, password or OTP, and no longer scrapes the portal's pages.
- `scripts/verify_login.py` uses the same browser sign-in and writes nothing
  to disk.

### Removed
- Email/password and OTP steps, and the debug HTML dumps they wrote to disk.

## 0.7.0
### Added
- Current time band sensor.
- Guidance in the POD card when there is no data to show.

### Changed
- Clearer sign-in and action error messages, in English and Italian.
- The last day sensors no longer go back to unknown.

### Fixed
- Sign-in and access issues ([#3](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/3),
  [#4](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/4),
  [#5](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/5),
  [#6](https://github.com/fabioscarparo/HomeAssistant-EDistribuzione/issues/6)).

## 0.6.0
### Fixed
- When private.e-distribuzione.it is blocked by Imperva's anti-bot check, login and token
  refresh fall back to the canonical Salesforce host edistribuzione.my.site.com, which is not
  behind Imperva. The custom domain stays the primary, so the integration returns to it on its
  own once the block is lifted. Thanks to @paki81 for finding the alternative host.

## 0.5.2
### Changed
- *Report issue* in Home Assistant now opens this repository's issues, instead of the original
  repository's.

## 0.5.1
### Fixed
- Once ignored, the notice about E-Distribuzione blocking automated access stayed hidden even
  after reloading the integration while the block lasted. It now shows again after every
  reload or restart as long as access is blocked, and disappears when the integration is
  removed.

## 0.5.0
First release of this fork of
[maurobraggio/HomeAssistant-EDistribuzione](https://github.com/maurobraggio/HomeAssistant-EDistribuzione).

### Added
- F1 / F2 / F3 time bands: three more statistics for the consumption direction
  (`edistribuzione:<pod>_energia_f1`, `_f2`, `_f3`), calculated from the 15-minute samples
  with the ARERA holiday calendar (October 4 included from 2026). Hour by hour they add up to
  the total, cover all the history already downloaded and follow corrections on their own.
- *POD card* (`custom:edistribuzione-pod-card`): consumption, return to grid and the F1/F2/F3
  split in day, week, month and year views, with Home Assistant's own look in light and dark
  mode. The integration loads it by itself, with no resource to add.
- Italian and English translations: entity names, setup and options, actions and error
  messages follow each user's language.
- Integration icon in Home Assistant, from version 2026.3.
- A notice in *Settings > Repairs* when E-Distribuzione blocks automated access with its
  anti-bot check; it disappears on its own once access works again.

### Changed
- README in English, with a step-by-step guide to setup, history import and checks.

### Fixed
- Sensors missing until a restart when E-Distribuzione did not answer while Home Assistant
  was starting.
- When E-Distribuzione answers with its anti-bot check, setup no longer fails with the generic
  "cannot connect" error and updates no longer log an unexpected error: the integration
  reports the block and retries once an hour.
