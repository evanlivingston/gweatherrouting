<!-- Fork review template. Remove instructions and fields that do not apply.
     Keep this fork-specific template out of upstream contribution branches. -->

## Problem and resulting behavior

Describe the concrete trigger, what changes for the user, and material limits.

## Validation

List the relevant checks and results. Identify failures reproduced on the base
branch separately from new failures. For performance work, include measured
results and fidelity/parity checks.

## Test coverage

- [ ] Tests cover distinct behavior or safety risks; no redundant method-by-method cases were added.
- [ ] Fixtures and mocks match the production interfaces they represent.
- [ ] Any removed or updated test was obsolete, invalid, or covered elsewhere; unique safety coverage remains.
- [ ] Validation is targeted; full-suite runs are reported only when required or requested.

**Risk covered by tests added/removed:**

## Integration and upstream plan

- Base branch and prerequisites:
- Merge order or retargeting needed:

This PR is for review in our fork. Merging requires user approval.

## AI assistance

Disclose any AI-assisted implementation, tests, or PR preparation. Human review
and responsibility remain with the contributor.
