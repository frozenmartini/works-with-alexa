# Works with Alexa

A Home Assistant custom integration for devices that only exist in Alexa: things with no
native Home Assistant integration, reachable only through Amazon's **unofficial** Alexa
account API (the one the Alexa app uses, not the official Smart Home Skill API).

It signs in as the iOS Alexa app, keeps only a refresh token, and exposes supported device
features Alexa reports as a **raw** Home Assistant entity: one entity per feature, named as
Alexa names it, with Alexa's own options and bounds. Assembling those raw entities into
whole devices is left to you, or to its companion integration, `kitbash`.

> Unofficial API. Amazon can change or block it at any time. Nothing here is endorsed by
> Amazon.

## What you get

One entity per Alexa feature, for the devices you pick on the integration's options page:

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

Only endpoints with supported capabilities are offered, regardless of category
or manufacturer. An application endpoint with power controls can be selectable;
an Echo Show with a temperature value can yield a sensor. Scenes have a separate
picker during setup and **Manage scenes** in Options. They start entirely
unselected, including on existing installations. Selected scenes appear as individual `button` entities grouped
under a single **Scenes** device card. Pressing a button activates its Alexa scene;
it has no on/off state. Adding all devices does not select any scenes.

Every raw endpoint entity carries a `reachable` attribute (Alexa's own connectivity flag). While a device
is off, its writable numbers and selects read unknown rather than the `0` Alexa reports.

After setup, open the entry's **Options** (gear) in Settings → Devices & services →
Works with Alexa. **Manage devices** lets you add, change or remove selections;
**Manage scenes** edits scene buttons independently. **Polling interval** changes
only the interval. Saving reloads this integration without signing in again.

Development installs can write rotating JSONL logs when their private `.dev-install`
marker specifies `evidence_log_dir`, relative to HA's configuration directory.
The logs cover all integration modules, API request timing, scene acknowledgements,
failures, polling, authentication refresh and option changes. Network requests and
their acknowledgements share request IDs. Credentials, headers, authentication
payloads and raw response bodies are excluded. Normal installations without this
marker keep Home Assistant's standard logging behavior.

## Signing in

Four connection paths and a separate login experiment, offered on the first screen:

1. **Borrow from Alexa Media Player** — if that integration is installed, reuse its refresh
   token. Nothing to type. The most reliable path today.
2. **Paste a credential bundle** — the JSON that `alexa-cookie2` or similar tools produce.
3. **Direct Amazon sign-in (experimental)** — open Amazon in your own browser, finish
   sign-in and any verification, then paste the full `/ap/maplanding` redirect URL
   into Home Assistant. For this trial, credentials are saved separately in
   `.storage/works_with_alexa_direct_experiment_<experiment-id>` with a name;
   earlier experiments are kept and no entry is created or changed.
   Your password and one-time code stay on Amazon. This follows
   the enrollment approach demonstrated by [alexapy-secure](https://github.com/superbeetle1973/alexapy-secure);
   recognized-browser enrollment, incognito enrollment with phone 2FA, independent
   API probes, and a later refresh-only call all succeeded live. This option
   remains an isolated experiment rather than a normal entry sign-in.
4. **Email, password and 2FA key** — the legacy relay flow: Amazon's pages open
   through Home Assistant with your details filled in, including the one-time code generated
   from your authenticator-app key. Nothing you type is stored. Fresh registration
   through this path has encountered Amazon risk challenges; borrowing an existing
   token does not demonstrate that fresh registration will succeed.
5. **Blank sign-in** — Amazon's pages with nothing filled in. Currently blocked by Amazon's
   risk check on fresh sessions; kept for when that changes.

Connection entries store only the refresh token, `macDms` and a device serial.
Access tokens, cookies and the CSRF token are rebuilt on every start and never saved. An
authentication failure triggers Home Assistant's reauthentication flow; stored credentials are
never deleted on an error.

## Capability matching

`features.py` centrally decides what the picker offers and which entities the
platforms create. Manufacturer and accessory category do not restrict it:

- `power`/`toggle` with on and off commands → switch.
- `mode` with `setMode` and advertised options → select, preserving Alexa's labels.
- `range` with `setRangeValue` and bounds → number, preserving bounds and step.
- Thermostat setpoints/modes → numbers/selects when their setters are advertised.
- `lock` with lock/unlock commands → lock.
- Read-only values and action status → sensors.

Thus a fan's speed can be a select or a number depending on its capability.
Anthem and Anthem Plus expose their advertised power controls as switches even
though Alexa categorizes them as OTHER. Unknown protocols are omitted until
implemented; an operation name alone is insufficient to import an unsupported
control. All entities still require selection through the picker. Selecting a
device or setting up the integration sends no control commands.

Every stateful writable entity updates optimistically before its API request:
switches, selects, numbers, thermostat setpoints/modes and locks. Failed commands
roll back without overwriting newer requests; a matching poll confirms the value.
Unconfirmed assumptions expire after 30 seconds, independently of the polling
interval. Read-only sensors keep their reported state. Scene buttons are stateless
and report the request acknowledgement or failure.

One account-wide endpoint query plus paginated scene discovery runs per polling
interval (default 60 seconds, minimum 15 seconds).

## What is verified

| Operation | Status |
|---|---|
| Discovery and capability classification | Verified with live read-only queries |
| Direct browser enrollment, phone 2FA and token refresh | Verified live in separate experimental storage |
| Close Vanes scene activation | Owner confirmed physical closure; API returned a placeholder acknowledgement |
| Scene buttons, options management and development logs | Loaded live; owner confirmed add/edit/remove; scene acknowledgements recorded |
| Latest capability mapping and optimistic concurrency/expiry | Automated checks passed; copied to development install, Core restart pending |
| Fan on/off, speed (mode), direction, toggles, range numbers | Verified live |
| Thermostat setpoints and mode | Implemented, **not yet executed** against a device |
| Lock / unlock | Implemented, **not yet executed** against a device |
| Vacuum actions (start, dock) | Status is read; actions are **not driven** — payload unknown |

## Install

**HACS** — Integrations → ⋮ → Custom repositories, add this repository as an *Integration*,
then download **Works with Alexa** and restart Home Assistant. (Once this repository is in the
HACS default store, it is searchable there directly and no custom repository is needed.)

**Manually** — copy `custom_components/works_with_alexa/` into `<config>/custom_components/`
and restart Home Assistant.

Either way, finish in Settings → Devices & services → Add integration → **Works with Alexa**.

The declared minimum is Home Assistant **2024.6.0**. This development session
used **2026.9.4**; the minimum needs revalidation for the new options and platform
changes before release. No Python requirements beyond Home Assistant itself.

## Development

Pure logic (parsing, classification, protocol handling, TOTP) carries no Home Assistant
imports, so it is testable with the standard library alone.

`features.py` is the single place the feature-to-entity rules live. Change the rules there and
not at a call site — the platforms, the device picker and the tests all read it.

The test suite is maintained alongside this repository rather than inside it, and runs against
fixtures captured from a live account. It is not distributed with the integration.
