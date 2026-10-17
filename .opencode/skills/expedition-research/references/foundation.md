# Reusable static-analysis foundation

## Existing analysis recovered on 2026-10-17

Recovered from tool commands in primary session
`ses_ec32cf927ffekFmCQvCyxLmsCq`, messages
`msg_1428e7c85001290IgcOnHAyLyE` and `msg_1428ea60f001swbbrUStkdokYg`.
The following locations were then confirmed with directory reads:

- Ghidra project: `/tmp/exp_ghidra/project/Exp_12_10_3.gpr` and adjacent
  `Exp_12_10_3.rep/`.
- Copied import binaries: `/tmp/exp_ghidra/imports/Expedition.exe` and
  `/tmp/exp_ghidra/imports/ExpDLL.dll`.
- Existing imports/runtime-string catalogs: `/tmp/exp_ghidra/catalog/`.
- Targeted reconstructed pseudocode: `/tmp/exp_ghidra/decompiled/`, including
  GRIB, polar, routing and tide subdirectories, plus earlier function lists.
- Existing exporter scripts and xref outputs: `/tmp/exp_ghidra/`.

Do not rerun old scripts blindly: some hard-code output into the original
analysis tree. Treat it as read-only. Do not inspect licensing/access-control
traces for bypass purposes. Historical pseudocode is not original source;
verify module association and actual code before making behavioral claims.
The old `_function_list.txt` is a name list, not an address-qualified verified
function index. A project file's presence does not prove its analysis is
complete or that every module was imported.

## Cache location

`/private/var/folders/w7/hb5wv1x1715821rqd1_3xf4r0000gn/T/opencode/expedition-analysis-cache/`

- `LATEST`: path to the most recent completed `inventory.json`.
- `inventories/<UTC-run>/`: immutable per-run inventory, MSI File/Component/
  Directory/Property table exports, and tool versions.
- `modules/<SHA-256>/`: PE metadata, import/export/header listing, automatically
  indexed AMD64 unwind ranges, optional reusable disassembly and named-address
  extracts. Each tool-produced listing has a `.command.json` provenance file.
- `prior-exports/<content-SHA-256>/`: snapshots of existing `.c`/`.txt` analysis
  exports. The inventory records original paths, snapshot paths and hashes.

The inventory maps installer-ID filenames to installed module names, records
MSI versions separately from executable metadata, and compares copied import
binaries with current module hashes. It does not assume the old database's
program association has been verified. The automatic range index is **not**
complete function discovery, a call graph, or verified semantic names. Some
functions lack unwind entries; entries may also describe function fragments.
No version is inferred from build timestamps.

Both original analysis and cache currently live in temporary directories.
Snapshots protect small exports from loss of the old tree, but do not back up
the Ghidra database or executable inputs. Do not describe this as a durable
backup. Relocation/backup to a permanent user-chosen directory is separate work.
The repository stores scripts/instructions, not binaries or bulk analysis.

### Initial completed build

Inventory run: `20261017T172634230572Z` (UTC). Generated:

- 27 application/provider PE modules, mapped to installed filenames.
- 124,709 automatically indexed AMD64 unwind ranges across those modules.
- 104 historical export snapshots.
- Full linear disassembly for `Expedition.exe` and `NavSdk2D.dll` only.
- Matching copied-input hashes for the old `Expedition.exe` and `ExpDLL.dll`.
- Three small native ENC query/display/formatting extracts.

`trace-leads.json` records those query/display addresses and the earlier
research session, keyed to the application hash. The extracts exist in that
module's cache directory. They are starting points, not newly verified
semantic function names. There is no complete verified behavioral function
map yet; build one incrementally from inspected evidence rather than inventing
names for every automatically discovered range.

## Build or refresh (parent, approved shell only)

From the repository root, using existing `msiinfo` and `objdump`:

```sh
python3 .opencode/skills/expedition-research/scripts/build_foundation.py \
  --output /private/var/folders/w7/hb5wv1x1715821rqd1_3xf4r0000gn/T/opencode/expedition-analysis-cache \
  build --artifacts /tmp/exp_inspect --prior /tmp/exp_ghidra \
  --installer /Users/elivingston/Downloads/ExpeditionSetup_12_10_3.exe \
  --disassemble Expedition.exe --disassemble NavSdk2D.dll
```

The initial scope indexes PE files in `a2out/` and `a3out/`; it excludes the
bootstrapper and VC++ redistributable from application-module discovery.
Installer and MSI identities are recorded separately. Only the two specified
modules receive full linear disassembly; other modules are inventoried and
can be added by installed name if needed. Existing disassembly is reused by
binary hash. No target execution, recursive auto-analysis, full decompilation,
tool installation or network access is performed.

## Reuse a targeted extract

```sh
python3 .opencode/skills/expedition-research/scripts/build_foundation.py \
  --output /private/var/folders/w7/hb5wv1x1715821rqd1_3xf4r0000gn/T/opencode/expedition-analysis-cache \
  extract --module Expedition.exe --start 0x1400ea120 --stop 0x1400ea821
```

Addresses are preferred-image VAs; metadata gives image base and range index
uses RVAs. Source hash is checked before extraction. Repeated identical ranges
reuse the existing extract. Boundaries supplied by an analyst need not align
with complete instructions/functions; interpret excerpts accordingly.

Before citing a historical trace, select the correct module hash and verify
the actual instructions. New verified index entries should include module
hash, precise addresses, scope, supporting extracts, provenance and evidence
label. Store unverified session leads separately, not as verified entries.
