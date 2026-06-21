<!--
Thanks for contributing! Please read CONTRIBUTING.md before opening a PR.
The contribution rules are strict — every change must respect the hard
invariants in docs/INVARIANTS.md, ship test coverage for the affected path,
and (for new detection rules) bring real-data evidence.
-->

## Summary

<!-- 1-3 bullets: what changed and why. -->

## Type of change

- [ ] Bug fix (non-breaking change that fixes an issue)
- [ ] New feature / tunable (non-breaking)
- [ ] Docs / packaging / CI only
- [ ] Touches a hard invariant (see docs/INVARIANTS.md — needs an issue first)

## Test plan

<!-- What did you run? Reviewers want to see this before merging. -->

- [ ] `just lint` passes (ruff + syntax + version-check)
- [ ] `just test` passes (unit suite)
- [ ] `just e2e` passes (e2e suite — only if behaviour changed)
- [ ] If touching detection thresholds: real-data evidence attached (incident timestamp, `delaybuf.log` excerpt, fires-vs-misses analysis)
- [ ] If touching the plugin: smoke-tested on a real Dispatcharr instance

## Linked issues

<!-- Closes #XXX, Refs #YYY -->
