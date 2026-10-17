#!/usr/bin/env python3
"""Build a local static-analysis cache; never execute an inspected binary."""

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
from datetime import datetime, timezone

SCHEMA = 1


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def capture(command, output):
    """Keep potentially large tool output out of the conversation."""
    partial = output.with_name(output.name + ".partial")
    try:
        with partial.open("wb") as stream:
            result = subprocess.run(
                command, stdout=stream, stderr=subprocess.PIPE, timeout=300, check=False
            )
        if result.returncode:
            raise RuntimeError(
                f"{command[0]} failed: {result.stderr.decode(errors='replace')[:1000]}"
            )
        partial.replace(output)
        write_json(output.with_name(output.name + ".command.json"), command)
    finally:
        partial.unlink(missing_ok=True)


def pe_index(path):
    """Index PE headers and x64 unwind ranges, not guessed semantic functions."""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return None
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe : pe + 4] != b"PE\0\0":
        return None
    machine, count = struct.unpack_from("<HH", data, pe + 4)
    opt_size = struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    if magic not in (0x10B, 0x20B):
        raise ValueError(f"Unsupported PE optional header: {path}")
    is64 = magic == 0x20B
    base = struct.unpack_from("<Q" if is64 else "<I", data, opt + (24 if is64 else 28))[
        0
    ]
    sections = []
    for i in range(count):
        off = opt + opt_size + 40 * i
        virtual_size, rva, raw_size, raw = struct.unpack_from("<IIII", data, off + 8)
        sections.append(
            {
                "name": data[off : off + 8].rstrip(b"\0").decode(errors="replace"),
                "rva": rva,
                "virtual_size": virtual_size,
                "file_offset": raw,
                "file_size": raw_size,
            }
        )

    def file_offset(rva):
        for section in sections:
            delta = rva - section["rva"]
            if 0 <= delta < section["file_size"]:
                return section["file_offset"] + delta
        return None

    ranges = []
    # Exception directory (entry 3): AMD64 RUNTIME_FUNCTION triples.
    directories = opt + (112 if is64 else 96)
    directory_count = struct.unpack_from("<I", data, directories - 4)[0]
    if machine == 0x8664 and directory_count > 3:
        rva, size = struct.unpack_from("<II", data, directories + 3 * 8)
        off = file_offset(rva) if rva else None
        if off is not None:
            if off + size > len(data) or size % 12:
                raise ValueError(f"Invalid exception directory: {path}")
            for pos in range(off, off + size, 12):
                begin, end, unwind = struct.unpack_from("<III", data, pos)
                if begin and end > begin:
                    ranges.append((begin, end, unwind, file_offset(begin)))
    metadata = {
        "format": "PE32+" if is64 else "PE32",
        "machine": hex(machine),
        "image_base": hex(base),
        "entry_rva": hex(struct.unpack_from("<I", data, opt + 16)[0]),
        "sections": sections,
        "unwind_range_count": len(ranges),
        "range_semantics": "Runtime unwind ranges; not complete function discovery or verified names",
    }
    return metadata, ranges


def msi_mapping(msi, cache):
    tool = shutil.which("msiinfo")
    if not tool:
        raise RuntimeError("msiinfo is required for installer filename mapping")
    mappings = {}
    for table in ("File", "Component", "Directory", "Property"):
        output = cache / f"msi-{table}.tsv"
        capture([tool, "export", str(msi), table], output)
        lines = output.read_text(errors="replace").splitlines()
        if table == "File":
            for row in csv.DictReader(
                io.StringIO("\n".join([lines[0]] + lines[3:])), delimiter="\t"
            ):
                mappings[row["File"]] = {
                    "installed_name": row["FileName"].split("|")[-1],
                    "msi_version": row.get("Version", ""),
                    "component": row.get("Component_", ""),
                }
    return mappings


def ensure_separate(output, inputs):
    for source in inputs:
        if output == source or output in source.parents or source in output.parents:
            raise ValueError(f"Cache and input must not contain each other: {source}")


def build(args):
    root = args.artifacts.resolve(strict=True)
    prior = args.prior.resolve(strict=True)
    output = args.output.resolve()
    msi = root / "main/a0"
    ensure_separate(output, [root, prior, args.installer.resolve(strict=True)])
    if not shutil.which("objdump"):
        raise RuntimeError("objdump unavailable; do not install tools automatically")
    output.mkdir(parents=True, exist_ok=True)
    # Preserve every generation instead of mutating an existing inventory.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run = output / "inventories" / stamp
    run.mkdir(parents=True)
    mapping = msi_mapping(msi, run)
    tools = {}
    for name in ("objdump", "msiinfo"):
        path = shutil.which(name)
        version = run / f"{name}-version.txt"
        capture([path, "--version"], version)
        tools[name] = {
            "path": path,
            "version": version.read_text(errors="replace").strip(),
        }
    modules = []
    for directory in (root / "a2out", root / "a3out"):
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                if stream.read(2) != b"MZ":
                    continue
            info = pe_index(path)
            if info is None:
                continue
            metadata, ranges = info
            sha = digest(path)
            module = output / "modules" / sha
            module.mkdir(parents=True, exist_ok=True)
            installed = mapping.get(path.name, {"installed_name": path.name})
            write_json(module / "pe.json", metadata)
            with (module / "unwind-ranges.tsv").open("w") as stream:
                stream.write("begin_rva\tend_rva_exclusive\tunwind_rva\tfile_offset\n")
                for begin, end, unwind, off in ranges:
                    stream.write(
                        f"{begin:#x}\t{end:#x}\t{unwind:#x}\t{'' if off is None else hex(off)}\n"
                    )
            headers = module / "headers-imports-exports.txt"
            if not headers.exists():
                capture([shutil.which("objdump"), "-p", str(path)], headers)
            disassembly = module / "disassembly.txt"
            if (
                installed["installed_name"] in args.disassemble
                and not disassembly.exists()
            ):
                capture([shutil.which("objdump"), "-d", str(path)], disassembly)
            record = {
                "path": str(path),
                "sha256": sha,
                "size": path.stat().st_size,
                **installed,
                **metadata,
                "cache": str(module),
                "disassembly_cached": disassembly.exists(),
            }
            modules.append(record)
    found = {m["installed_name"] for m in modules}
    missing = set(args.disassemble) - found
    if missing:
        raise ValueError(f"Requested modules not found in mapping: {sorted(missing)}")

    # Associate historical exports with known matching copied input hashes,
    # never with a version label or a function address alone.
    copied_inputs = []
    for path in sorted((prior / "imports").iterdir()):
        if path.is_file():
            sha = digest(path)
            copied_inputs.append(
                {
                    "path": str(path),
                    "sha256": sha,
                    "matches_module": sha in {m["sha256"] for m in modules},
                }
            )
    reuse = []
    for folder in ("catalog", "decompiled"):
        for path in sorted((prior / folder).rglob("*")):
            if path.is_file() and path.suffix in (".c", ".txt"):
                sha = digest(path)
                snapshot = output / "prior-exports" / sha / path.name
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                if not snapshot.exists():
                    shutil.copyfile(path, snapshot)
                reuse.append(
                    {
                        "path": str(path),
                        "snapshot": str(snapshot),
                        "sha256": sha,
                        "size": path.stat().st_size,
                    }
                )
    inventory = {
        "schema": SCHEMA,
        "created_utc": stamp,
        "modules": modules,
        "installer": {
            "path": str(args.installer.resolve()),
            "sha256": digest(args.installer),
        },
        "msi": {"path": str(msi), "sha256": digest(msi)},
        "prior_analysis": {
            "project": str(prior / "project/Exp_12_10_3.gpr"),
            "copied_inputs": copied_inputs,
            "exports": reuse,
            "trust": "Historical exports, not reverified behavioral findings; database program association not independently checked",
        },
        "tools": tools,
        "command": {
            "artifacts": str(root),
            "prior": str(prior),
            "disassemble": args.disassemble,
        },
    }
    write_json(run / "inventory.json", inventory)
    (output / "LATEST").write_text(str(run / "inventory.json") + "\n")
    print(
        json.dumps(
            {
                "inventory": str(run / "inventory.json"),
                "modules": len(modules),
                "prior_exports": len(reuse),
                "disassembly_cached": sum(m["disassembly_cached"] for m in modules),
            }
        )
    )


def extract(args):
    inventory = json.loads(
        Path((args.output / "LATEST").read_text().strip()).read_text()
    )
    matches = [
        m
        for m in inventory["modules"]
        if args.module in (m["installed_name"], m["sha256"])
    ]
    if len(matches) != 1:
        raise ValueError("Select exactly one module by installed name or full SHA-256")
    module = matches[0]
    source = Path(module["path"])
    if digest(source) != module["sha256"]:
        raise ValueError("Input changed; rebuild inventory before extracting")
    if args.start >= args.stop:
        raise ValueError(
            "start must be less than stop (preferred-image virtual addresses)"
        )
    target = Path(module["cache"]) / "extracts" / f"{args.start:x}-{args.stop:x}.asm"
    target.parent.mkdir(exist_ok=True)
    if not target.exists():
        capture(
            [
                shutil.which("objdump") or "objdump",
                "-d",
                f"--start-address={args.start:#x}",
                f"--stop-address={args.stop:#x}",
                str(source),
            ],
            target,
        )
    print(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("build")
    inventory.add_argument("--artifacts", type=Path, required=True)
    inventory.add_argument("--prior", type=Path, required=True)
    inventory.add_argument("--installer", type=Path, required=True)
    inventory.add_argument("--disassemble", action="append", default=[])
    snippet = commands.add_parser("extract")
    snippet.add_argument("--module", required=True)
    snippet.add_argument("--start", type=lambda v: int(v, 0), required=True)
    snippet.add_argument("--stop", type=lambda v: int(v, 0), required=True)
    args = parser.parse_args()
    (build if args.command == "build" else extract)(args)


if __name__ == "__main__":
    main()
