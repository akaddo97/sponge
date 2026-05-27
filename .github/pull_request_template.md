## What this changes

One or two sentences. What is different after this PR lands?

## Why

What problem does this solve, or what gap does it close? Link to an issue if there is one.

## How

The shape of the change. Files touched, behaviour preserved, any migration the user has to do.

## Verification

- [ ] `pytest` is green locally
- [ ] CI is green on the PR branch
- [ ] If this touches the voice pipeline, I tested at least one round-trip from `voice_inbox/` to provisional commit
- [ ] If this touches the `GraphBackend` Protocol, I ran the in-repo backend tests and updated any docstring contracts
- [ ] No real-graph data committed (no fixtures lifted from a personal graph)

## Notes for the reviewer

Anything not obvious from the diff — risk areas, follow-ups deferred, open questions.
