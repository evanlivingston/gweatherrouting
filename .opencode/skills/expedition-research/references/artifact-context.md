# Recovered Expedition artifact context

Recovered on 2026-10-17 at the user's request. This is a discovery index,
not evidence of Expedition behavior or a grant of shell approval. Preserve
the skill's static, read-only constraints and obtain required approval.

## Primary binary-analysis session

- Session ID: `ses_ec32cf927ffekFmCQvCyxLmsCq`
- Title: `New session - 2026-10-14T23:50:25.240Z`
- Session working directory: `/Users/elivingston/gweatherrouting`
- Initial user message: `msg_13cd306eb001mlyQd2iTLw51EF`
- Initial request identified
  `/Users/elivingston/Downloads/ExpeditionSetup_12_10_3.exe`
  for inspection.

This is the actual installer/binary-analysis session. Its history includes
extraction, static analysis/decompilation, and discussions of GRIB selection
and tides/currents. Recover commands and candidate addresses from it when
useful; do not cite its answers as code-confirmed findings. No original
Expedition source tree was identified during this recovery. Decompiled
pseudocode is not original source code.

## Known local locations

| Role | Path |
| --- | --- |
| Original installer | `/Users/elivingston/Downloads/ExpeditionSetup_12_10_3.exe` |
| Existing extraction root | `/tmp/exp_inspect/` |
| Extracted application binary | `/tmp/exp_inspect/a2out/Expedition` |
| Application/provider payload files, many with installer IDs rather than DLL names | `/tmp/exp_inspect/a2out/` |
| Outer payloads | `/tmp/exp_inspect/main/` |
| Additional extracted payloads | `/tmp/exp_inspect/a3out/` |
| Bootstrapper payloads | `/tmp/exp_inspect/ux/` |
| Carved cabinets | `/tmp/exp_inspect/main.cab`, `/tmp/exp_inspect/ux.cab` |

The extraction root, listed subdirectories/cabinets, and application filename
were confirmed present with directory reads during recovery. The original
installer was identified in session history, not freshly checked or hashed.
Temporary paths can disappear; recheck before analysis. Existing artifacts
are inputs: do not overwrite or re-extract into this tree. Put any new
derived output in a separately approved temporary directory, preferring the
environment's approved OpenCode temporary root over generic `/tmp`.

Historical extraction mapping from session tool output:

- `main/a0`: MSI installation database.
- `main/a1`: Visual C++ redistributable installer, not Expedition itself.
- `main/a2`: primary application cabinet; extracted under `a2out/`.
- `main/a3`: additional cabinet; extracted under `a3out/`.

Resolve installer-ID filenames through the MSI file/directory tables or
binary metadata before assigning module/provider names. The later foundation
recovery located the existing Ghidra project and exports; see
`foundation.md` before regenerating any analysis.

## Historical installer identity (revalidate)

The primary session's tool output recorded:

- Size: `126978088` bytes.
- SHA-256: `8f9ef60fb4a4444e535aedb87149662b43541a4dc3e7579290897abcd0afb6d6`.
- PE32+, x86-64 Windows installer; embedded version `12.10.3.0` / `12.10.3`.
- Subsequent inspection identified a WiX Burn bundle; the initial `file`
  output's InstallShield label was not the final identification.

These identify the installer only. Establish separate hashes, architecture,
and versions for the extracted application and each relevant provider DLL.
Do not generalize findings to other Expedition versions or application variants.

## Other sessions: do not confuse with binary analysis

- `ses_eb8be5a36ffezC4njyZ275pRW6`, **Expedition chart quilting and scaling**:
  later charting discussion, not the primary reverse-engineering session.
- `ses_eb548d5afffev9dlWd7JljvAHy`, **Finding previous session on expedition
  software implementation details**: skill/agent setup and later research.
- `ses_eb54580c9ffe6yQ0dDSlS92yh5`, **Investigate Expedition cursor depth**:
  earlier documentation-based research; not eligible implementation evidence.

The six cursor-depth questions remain a research scope, not established
findings: value semantics; supported chart providers; interpolation and
terrain constraints; overlap selection; datum/tide/draft handling; and
mouse-update/display/settings paths. Verify them in the actual code paths.
