# works_with_alexa

A Home Assistant custom integration that controls Alexa-only smart devices through
Amazon's unofficial Alexa account API, exposing every device feature raw: one Home
Assistant entity per Alexa feature, nothing assembled.

This file is the instruction set for agents that read only this repository. Codex
discovers instructions from the git root downward, so everything an agent needs while
working in this checkout belongs here, not in a parent directory.

## Design rules

- **Raw layer only.** One entity per Alexa feature, using the plainest primitive:
  power/toggle to `switch`, mode to `select`, writable range to `number`, read-only
  value to `sensor`, lock to `lock`. **Do not add composite fan, climate, cover or
  vacuum entities** — assembling devices is a separate integration's job.
- `features.py` is the single source of truth for raw-feature classification. The
  platforms, the device picker and the tests all read it, and `tests/test_features.py`
  pins the contract. Change the rules there, not at a call site.
- Writes are **optimistic**: hold the value as the entity's state before the request
  leaves, until a poll confirms it or it times out, and roll back on failure. Amazon's
  round trip is slow enough that anything driving an entity faster will otherwise fight
  the user's input.
- Build this integration's own Amazon authentication. Never copy or reuse a session
  captured elsewhere.

## Tests

Stdlib only, no Home Assistant required:

```
python3 -m unittest discover -s tests -t .
```

## Safety

This integration is developed against a live, occupied home.

- **Never restart Home Assistant Core.** Say a restart is needed and stop.
- Some endpoints are blocked from writes by standing directive because they control
  water. Respect the block list; never add a write path for a blocked endpoint.
- Never print secrets, tokens or resolved credentials.
- Questions only a live account can answer are captured out of band, not guessed.
- Commit only when asked.
