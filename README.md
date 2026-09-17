# Works with Alexa

A Home Assistant custom integration for devices that only exist in Alexa: things with no
native Home Assistant integration, reachable only through Amazon's **unofficial** Alexa
account API (the one the Alexa app uses, not the official Smart Home Skill API).

It signs in as the iOS Alexa app, keeps only a refresh token, and exposes every device
feature Alexa reports as a **raw** Home Assistant entity: one entity per feature, named as
Alexa names it, with Alexa's own options and bounds. Assembling those raw entities into
whole devices is left to you, or to its companion integration, `kitbash`.

> Unofficial API. Amazon can change or block it at any time. Nothing here is endorsed by
> Amazon.

## What you get

Two layers, chosen separately on the device page of the integration's options:

**Raw devices.** One entity per Alexa feature, nothing assembled:

| Alexa feature | Home Assistant entity | Notes |
|---|---|---|
| `power`, `toggle` | `switch` | Power is named "Power"; toggles take Alexa's label (Auto, Mute, Oscillate…) |
| `mode` | `select` | Options are Alexa's labels (Low/Medium/High/Turbo, Exhaust/Direct, Auto/Natural/Normal/Sleep) |
| `range`, writable | `number` | Bounds and step are the device's own (0–6, 60–90, 0–100 Percent…) |
| `thermostat` | one `number` per setpoint + a "Thermostat Mode" `select` | Target, lower and upper setpoints as the device reports them; modes as Alexa lists them |
| `lock` | `lock` | Locked / unlocked / jammed |
| anything read-only with a value | `sensor` | Temperatures carry °F/°C; a vacuum's action status reads IDLE / RUNNING |

Nothing is assembled here: whole devices (a fan from a power switch, a speed select and a
direction select, say) are built from these entities by the companion Kitbash integration.
The built Window Fan `fan` entity that earlier versions carried was removed on 2026-09-13.

Only devices that would yield at least one entity are offered;
phones, apps, Echo speakers and scene triggers are filtered out.

Every entity carries a `reachable` attribute (Alexa's own connectivity flag). While a device
is off, its writable numbers and selects read unknown rather than the `0` Alexa reports.

## Signing in

Four paths, offered on the first screen:

1. **Borrow from Alexa Media Player** — if that integration is installed, reuse its refresh
   token. Nothing to type. The most reliable path today.
2. **Paste a credential bundle** — the JSON that `alexa-cookie2` or similar tools produce.
3. **Email, password and 2FA key** — the way Alexa Media Player signs in: Amazon's pages open
   through Home Assistant with your details filled in, including the one-time code generated
   from your authenticator-app key. Nothing you type is stored.
4. **Blank sign-in** — Amazon's pages with nothing filled in. Currently blocked by Amazon's
   risk check on fresh sessions; kept for when that changes.

Whatever the path, the entry stores only the refresh token, `macDms` and a device serial.
Access tokens, cookies and the CSRF token are rebuilt on every start and never saved. An
authentication failure triggers Home Assistant's reauthentication flow; stored credentials are
never deleted on an error.

## Safety

- **Kohler devices are never written to.** The Kohler Anthem is a shower valve; turning it on
  runs water. Its features appear as read-only sensors and every write path refuses it.
- Writable entities are created only for these Alexa device categories: fan, blind, lock,
  thermostat, vacuum cleaner. Everything else is sensors at most.
- Polling is one request per interval for the whole account (default 60 s, minimum 15 s).

## What is verified

| Operation | Status |
|---|---|
| Discovery and state for every entity type above | Verified against a live account |
| Fan on/off, speed (mode), direction, toggles, range numbers | Verified live |
| Thermostat setpoints and mode | Implemented, **not yet executed** against a device |
| Lock / unlock | Implemented, **not yet executed** against a device |
| Vacuum actions (start, dock) | Status is read; actions are **not driven** — payload unknown |

## Install

1. Copy this folder to `<config>/custom_components/works_with_alexa/`.
2. Restart Home Assistant Core.
3. Settings → Devices & services → Add integration → **Works with Alexa**.

No Python requirements beyond Home Assistant itself.

## Development

Pure logic (parsing, classification, protocol handling, TOTP) carries no Home Assistant
imports, so it is testable with the standard library alone.

`features.py` is the single place the feature-to-entity rules live. Change the rules there and
not at a call site — the platforms, the device picker and the tests all read it.

The test suite is maintained alongside this repository rather than inside it, and runs against
fixtures captured from a live account. It is not distributed with the integration.
