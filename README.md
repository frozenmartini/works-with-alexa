# Works with Alexa

A Home Assistant custom integration for devices that only exist in Alexa: things with no
native Home Assistant integration, reachable only through Amazon's **unofficial** Alexa
account API (the one the Alexa app uses, not the official Smart Home Skill API).

It signs in as the iOS Alexa app, keeps device-registration credentials, and exposes supported device
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
| `thermostat` | one `number` per setpoint + a "Thermostat Mode" `select` | Target, lower and upper setpoints as the device reports them; modes as Alexa lists them. Alexa gives no setpoint bounds, so these are fixed: 40–100 °F step 1, 5–38 °C step 0.5, 278–311 K step 0.5 |
| `lock` | `lock` | Locked / unlocked / jammed |
| anything read-only with a value | `sensor` | Temperatures carry °F, °C or K as Alexa reports; a vacuum's action status reads IDLE / RUNNING |

Nothing is assembled here: whole devices (a fan from a power switch, a speed select and a
direction select, say) are built from these entities by the companion Kitbash integration.
The built Window Fan `fan` entity that earlier versions carried was removed on 2026-09-13.

Only endpoints with supported capabilities are offered, regardless of category
or manufacturer; scene-trigger endpoints are never offered as devices. Devices
that yield only sensors are marked read-only in the picker. An application endpoint with power controls can be selectable;
an Echo Show with a temperature value can yield a sensor. Scenes have a separate
picker during setup and follow the device picker in Options. They start entirely
unselected, including on existing installations. Selected scenes appear as individual `button` entities grouped
under a single **Scenes** device card. Pressing a button activates its Alexa scene;
it has no on/off state. Adding all devices does not select any scenes.

Raw endpoint entities carry a `reachable` attribute (Alexa's own connectivity flag) when
Alexa reports the device's connectivity. While a device's power reports off, its writable
range numbers and mode selects read unknown rather than the `0` Alexa reports. Thermostat
setpoints and Thermostat Mode always show what Alexa reports.

After setup, open the entry's **Options** (gear) in Settings → Devices & services →
Works with Alexa. **Add, edit or remove devices** opens the device checkboxes,
with current selections checked. Click **Next** for the scene checkboxes, then
submit to save both selections. Unticking a device or scene removes its entities
from Home Assistant. **Polling interval** changes only the interval. Saving reloads
this integration, when anything changed, without signing in again.

Development installs can write JSONL logs when their private `.dev-install`
marker specifies `evidence_log_dir`, relative to HA's configuration directory.
`works_with_alexa.jsonl` covers all integration modules, API request timing, scene
acknowledgements, failures, polling and option changes, rotating at 5 MB with five
backups. `auth.jsonl` records sign-in, refresh and reauthentication events and is
kept whole, without rotation. Network requests and their acknowledgements share
request IDs. Credentials, headers and raw response bodies are excluded; the
authentication log keeps redacted summaries of Amazon's responses and one-way
fingerprints of tokens, never the tokens. Normal installations without this
marker keep Home Assistant's standard logging behavior.

## Signing in

Sign in directly on Amazon in your own browser:

1. Choose your Amazon marketplace in Home Assistant.
2. Open the Amazon sign-in link in a separate browser tab and complete sign-in
   and any verification there. Passwords and verification codes stay on Amazon.
3. Copy the full `/ap/maplanding` redirect URL into Home Assistant and submit.
   A blank or error page at that address is expected.
4. After your account is verified, choose devices individually, add all of them,
   or add none for now; then choose scenes, if the account has any.

If Amazon rejects the pasted URL, tick **Start a new sign-in** to get a fresh link.
If the device list cannot be fetched at the end of setup, setup finishes with every
supported device included; narrow it down later in Options.

To replace a sign-in, use **Options → Amazon sign-in**. That starts Home Assistant's
reauthentication; finish it from the integration in Settings → Devices & services.
The marketplace can be changed there; the polling interval stays in Options.
Reauthentication verifies that you signed in to the same account, retains device and
scene selections, and reuses the entry's device serial. If verification fails
temporarily after Amazon accepted the sign-in, retry the form without starting another
sign-in.

Older entries may have used a device serial as their account identifier. The
integration learns their account identity while the old sign-in still works,
keeping the existing entry and selections. If that sign-in has expired before
an identity was recorded, recovery shows the verified Amazon account and asks
you to explicitly confirm it is the account originally used for the entry.
Cancel if it is a different account. Known account mismatches are always rejected.

Connection entries store the refresh token, `macDms`, a device serial, the
marketplace and when and how you signed in. **No email address and no password is
stored.** The account is recorded only as a one-way SHA-256 key of its email, which
is how the integration recognises the same account at reauthentication and refuses
a second setup of it. The key cannot be turned back into the email, but anyone who
already knows an email can check whether it matches. Entries created by 0.1.0, which
stored the email, are converted the first time they load. Access tokens, cookies and
the CSRF token are rebuilt on every start and never saved. An authentication failure
triggers Home Assistant's reauthentication flow; stored credentials are never deleted
on an error.

Credential rotation belongs to the session that started it: a late refresh from
an older session cannot overwrite a completed replacement sign-in. Scheduled
renewal retries recoverable cookie/CSRF failures after 60 seconds; for scheduled
renewal, only confirmed root-credential rejection requests interactive sign-in.

## Capability matching

`features.py` centrally decides what the picker offers and which entities the
platforms create; read-only sensors come from `models.sensor_features`, which it
reuses. Manufacturer and accessory category do not restrict it:

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
interval. Read-only sensors keep their reported state. Scene buttons are stateless:
a failed activation shows an error, and Amazon's acknowledgement is recorded only in
development logs.

One account-wide endpoint query plus paginated scene discovery runs per polling
interval (default 60 seconds, minimum 15 seconds). A successful command, and opening
the device options, also request an immediate refresh.

## Optional discovery contract

Raw endpoint entities expose a namespaced `wwa_feature` dictionary in their
Home Assistant state attributes. Kitbash and other consumers can discover these
entities through HA's entity/device registries and state metadata. The integrations
remain independent: Kitbash does not need to import Works with Alexa or call Alexa.
This metadata is optional for consumers; ignore unfamiliar fields and do not assume
older installations or other integrations provide it. Scene buttons are separate
account automations and do not carry endpoint feature metadata.

| Field | Meaning |
|---|---|
| `key` | Existing stable Alexa feature key, such as `power`, `mode/1`, or `range/Fan.Speed` |
| `type` | Original feature name/type, such as `power`, `range`, `mode`, `toggle`, `thermostat`, or `temperatureSensor` |
| `instance` | Original instance (including numeric values), or `null` |
| `label` | Alexa's feature label, with the existing readable fallback when absent; unaffected by HA entity renaming |
| `operations` | Advertised operation names, as a sorted list; these may include operations the entity does not implement |
| `writable` | Whether this specific entity supports writing with its advertised commands; always `false` for sensors, including action-status sensors |
| `ordered` | `true` for explicitly ORDERED mode features, `false` for explicitly UNORDERED ones; `null` when unknown or not a mode feature |
| `property` | Thermostat child property (`targetSetpoint`, `lowerSetpoint`, `upperSetpoint`, or `thermostatMode`); `null` otherwise |
| `role` | Optional semantic hint, published only when supported by the capability, original label, or semantic instance; omitted when uncertain or conflicting |

For example, an oscillation-angle number can publish:

```json
{
  "wwa_feature": {
    "key": "range/Fan.OscillationAngle",
    "type": "range",
    "instance": "Fan.OscillationAngle",
    "label": "Oscillation Angle",
    "operations": ["adjustRangeValue", "setRangeValue"],
    "writable": true,
    "ordered": null,
    "property": null,
    "role": "oscillation_angle"
  }
}
```

Hints include `power`, `speed`, `oscillate`, `oscillation_angle`, `direction`,
`mode`, `temperature`, `setpoint`, `heating`, `cooling`, `lock`, `contact`, `light`,
and `cover`. Manufacturer, model, device name/category, numeric instances and
select option labels do not determine roles. An ordered mode alone does not
establish speed, and an unordered mode is not a speed scale. A range measuring
oscillation angle is distinct from an oscillation on/off toggle.

Thermostat children share their parent's feature `key` and `label`, with
`property` distinguishing them. Setpoint numbers carry `role: "setpoint"` and the
Thermostat Mode select carries `role: "mode"`. Temperature sensors are separate
`temperatureSensor` features, not thermostat children, and carry
`role: "temperature"`. A whole read-only
thermostat feature has no assumed temperature role. Consumers should identify
an entity through the HA registry and use `property` to distinguish thermostat
children, rather than treating the feature key alone as an entity identifier.

The dictionary merges with existing attributes such as `reachable`, `feature`,
and `reported_unit`. It contains no credentials or raw account payloads. It does
not change entity unique IDs, device grouping, select labels, numeric bounds,
units, or commands. If an endpoint disappears, its entities become unavailable, and
Home Assistant leaves out an unavailable entity's extra state attributes, so
`wwa_feature` and `reachable` are absent from state until the endpoint returns.
Check availability before writing.

## What is verified

| Operation | Status |
|---|---|
| Discovery and capability classification | Verified with live read-only queries |
| Direct browser enrollment, phone 2FA and token refresh | Verified live in separate experimental storage |
| Close Vanes scene activation | Owner confirmed physical closure; API returned a placeholder acknowledgement |
| Scene buttons, options management and development logs | Loaded live; owner confirmed add/edit/remove; scene acknowledgements recorded |
| Capability mapping and optimistic concurrency/expiry | Automated checks passed; mapping and optimistic confirmation observed live after Core restarts |
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

Requires Home Assistant **2026.3.0 or newer**, including support for the bundled
integration icon. No Python requirements beyond Home Assistant itself.

## Development

Pure logic (parsing, classification, protocol handling, registration) carries no Home Assistant
imports, so it is testable with the standard library alone.

`features.py` is where the feature-to-entity rules live, with the read-only sensor rule in
`models.sensor_features`. Change the rules there and not at a call site — the platforms, the
device picker and the tests all read them.

The test suite is maintained alongside this repository rather than inside it, and runs against
fixtures captured from a live account. It is not distributed with the integration.
