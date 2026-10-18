"""Small synthetic tests; no Expedition binaries or external tools required."""

import importlib.util
from pathlib import Path
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "foundation", Path(__file__).with_name("build_foundation.py")
)
foundation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(foundation)


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def pe(self):
        data = bytearray(1024)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3C, 0x80)
        data[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<HH", data, 0x84, 0x8664, 1)
        struct.pack_into("<H", data, 0x94, 240)
        opt = 0x98
        struct.pack_into("<H", data, opt, 0x20B)
        struct.pack_into("<I", data, opt + 16, 0x1000)
        struct.pack_into("<Q", data, opt + 24, 0x140000000)
        struct.pack_into("<I", data, opt + 108, 16)
        struct.pack_into("<II", data, opt + 112 + 24, 0x1100, 12)
        section = opt + 240
        data[section : section + 8] = b".text\0\0\0"
        struct.pack_into("<IIII", data, section + 8, 0x200, 0x1000, 0x200, 0x200)
        struct.pack_into("<III", data, 0x300, 0x1000, 0x1040, 0x1140)
        path = self.root / "synthetic.exe"
        path.write_bytes(data)
        return path

    def test_pe_ranges_and_base(self):
        info, ranges = foundation.pe_index(self.pe())
        self.assertEqual(info["image_base"], "0x140000000")
        self.assertEqual(ranges, [(0x1000, 0x1040, 0x1140, 0x200)])

    def test_non_pe(self):
        path = self.root / "data.txt"
        path.write_text("not executable")
        self.assertIsNone(foundation.pe_index(path))

    def test_truncated_exception_directory(self):
        path = self.pe()
        data = bytearray(path.read_bytes())
        struct.pack_into("<I", data, 0x98 + 112 + 24 + 4, 1008)
        path.write_bytes(data)
        with self.assertRaises(ValueError):
            foundation.pe_index(path)

    def test_output_input_overlap_rejected(self):
        source = self.root / "input"
        for output in (source, source / "cache", self.root):
            with self.assertRaises(ValueError):
                foundation.ensure_separate(output, [source])
        foundation.ensure_separate(self.root / "cache", [source])

    def test_capture_failure_keeps_existing_cache(self):
        output = self.root / "cached.txt"
        output.write_text("complete")
        with patch.object(
            foundation.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, stderr=b"bad"),
        ):
            with self.assertRaises(RuntimeError):
                foundation.capture(["fake-tool"], output)
        self.assertEqual(output.read_text(), "complete")
        self.assertFalse(output.with_name("cached.txt.partial").exists())

    def test_capture_timeout_cleans_partial(self):
        output = self.root / "cached.txt"
        with patch.object(
            foundation.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("fake-tool", 300),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                foundation.capture(["fake-tool"], output)
        self.assertFalse(output.exists())
        self.assertFalse(output.with_name("cached.txt.partial").exists())

    def test_msi_mapping_retains_version_and_long_name(self):
        def fake_capture(command, output):
            output.write_text(
                "File\tFileName\tVersion\tComponent_\nstring\tstring\tstring\tstring\nFile\tFile\nkey\tSHORT.DLL|Provider.dll\t1.2.3\tcomponent\n"
            )

        with (
            patch.object(foundation.shutil, "which", return_value="/fake/msiinfo"),
            patch.object(foundation, "capture", side_effect=fake_capture),
        ):
            mapping = foundation.msi_mapping(self.root / "input.msi", self.root)
        self.assertEqual(
            mapping["key"],
            {
                "installed_name": "Provider.dll",
                "msi_version": "1.2.3",
                "component": "component",
            },
        )

    def extract_args(self):
        source = self.pe()
        sha = foundation.digest(source)
        cache = self.root / "cache" / sha
        cache.mkdir(parents=True)
        inventory = self.root / "inventory.json"
        foundation.write_json(
            inventory,
            {
                "modules": [
                    {
                        "installed_name": "synthetic.exe",
                        "sha256": sha,
                        "path": str(source),
                        "cache": str(cache),
                    }
                ]
            },
        )
        (self.root / "LATEST").write_text(str(inventory))
        return (
            SimpleNamespace(
                output=self.root,
                module="synthetic.exe",
                start=0x140001000,
                stop=0x140001040,
            ),
            source,
            cache,
        )

    def test_extract_rejects_changed_input(self):
        args, source, _ = self.extract_args()
        source.write_bytes(source.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "Input changed"):
            foundation.extract(args)

    def test_extract_reuses_cached_range(self):
        args, _, cache = self.extract_args()
        target = cache / "extracts" / f"{args.start:x}-{args.stop:x}.asm"
        target.parent.mkdir()
        target.write_text("existing extract")
        with patch.object(foundation, "capture") as capture, patch("builtins.print"):
            foundation.extract(args)
        capture.assert_not_called()
        self.assertEqual(target.read_text(), "existing extract")

    def test_extract_rejects_reversed_range(self):
        args, _, _ = self.extract_args()
        args.stop = args.start
        with self.assertRaisesRegex(ValueError, "start must be less"):
            foundation.extract(args)


if __name__ == "__main__":
    unittest.main()
