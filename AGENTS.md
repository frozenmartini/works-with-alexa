# works_with_alexa

The shared active instructions are at [`../AGENTS.md`](../AGENTS.md). Before
acting, also read this directory's [`CLAUDE.md`](CLAUDE.md), which records the
current raw-feature contract and Amazon authentication protocol.

Expose Alexa capabilities as raw entities only; do not add composite fan,
climate, cover, or vacuum entities here. `features.py` is the source of truth
for raw-feature classification and its tests pin the contract. Build this
integration's own Amazon authentication; never copy the Pi's live session.
Questions requiring a live capture go to the documented sibling-repository
thread rather than being guessed.

Kohler Anthem is read-only. Never make or test a write/control path for it.
Do not restart Core yourself. The project uses `dumdum` as its prompt marker.
