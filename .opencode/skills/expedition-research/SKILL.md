---
name: Expedition Research
description: Research Expedition sailing software charting, routing, weather, polars, instruments, and data handling; separate documented behavior from implementation inference and compare with gweatherrouting.
---

# Expedition research

## Purpose and delegation

Investigate Expedition sailing/navigation software, not unrelated products
named Expedition. Prefer the `expedition-research` subagent for independent
research when it is available. If already running as that subagent, do the
research yourself; do not spawn another agent. A parent should supply the
specific question, relevant version/platform, and whether a code comparison
is wanted. Do not silently substitute another agent if this one is unavailable.

## Workflow

1. Identify the question and relevant feature. Ask for clarification only if
   ambiguity materially affects the answer; otherwise state your assumptions.
2. Read `references/prior-research.md` when chart selection, quilting, scale,
   or prior research is relevant. It contains leads, not verified findings.
3. Search for first-party evidence: official Expedition manuals/help,
   versioned release notes, and identifiable developer statements. Discover
   and verify official links rather than guessing documentation paths.
4. Fetch and read sources before citing them. Search snippets are discovery
   aids, not sufficient evidence. Record document/page title, URL, section or
   PDF page, and software version/date where available. Note access limits.
5. Use forums, demonstrations, or third-party comparisons as secondary
   evidence with explicit attribution. Prefer first-party evidence when
   sources conflict; report unresolved version-dependent differences.
6. Distinguish what users can observe from what the implementation actually
   does internally. Public documentation of a UI feature does not establish
   its algorithm, data structures, threading, GPU use, or cache architecture.
7. If requested, inspect the current gweatherrouting checkout and cite file
   paths and line numbers. Separate existing behavior from proposed changes.
   Do not modify code or start implementation without a separate request.
8. Stop when the question is answered or public evidence is exhausted. Report
   unknowns and the next useful verification step instead of inventing detail.

## Evidence labels

- **Documented:** explicitly supported by a cited first-party source.
- **Observed/reported:** visible in an identified demonstration or attributed
  to a user report; not necessarily general behavior across versions.
- **Inferred:** a reasoned implementation hypothesis, with supporting evidence
  and plausible alternatives. Never describe it as confirmed internals.
- **Unknown:** insufficient accessible evidence.

An earlier chat, repeated claim, or plausible architecture is not independent
confirmation. Keep direct quotations short and exact. Do not bypass access
controls or assume permission to inspect proprietary binaries/source.

## Report format

- **Answer:** direct summary with evidence labels and citations next to claims.
- **Evidence:** compact source list with relevant sections/pages and versions.
- **Comparison:** only when requested; current code references, gaps, and
  explicitly proposed design options, not claims about Expedition's internals.
- **Unknowns:** remaining uncertainties and concrete verification steps.

For safety-relevant routing, datum, depth, or weather behavior, highlight
unverified assumptions. Research findings are not navigational assurances.
