"""Independent end-to-end, publication and source-integrity attack tests."""

from __future__ import annotations

import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

import mastervault as mv
from test_formats import wav_bytes, dsf_bytes, dff_bytes


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mastervault-archive-tests-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.work = self.root / "scratch"
        self.work.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, data):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def pack(self, output=None, **kwargs):
        return mv.pack([self.source], output or self.root / "archive.mva",
                       codecs="stdlib", work_dir=self.work, average=16_384, **kwargs)

    def source_map(self):
        return {self.source.name + "/" + p.relative_to(self.source).as_posix(): p
                for p in self.source.rglob("*") if p.is_file()}

    def assert_clean(self):
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertEqual(list(self.root.glob(".mastervault-*")), [])

    def small_mixed_corpus(self):
        rng = random.Random(533)
        a = b"\x00\x00\x80\xff\xff\x7f\x01\x00\x00\xff\xff\xff" * 4000
        b = b"\x7d\x00\x00\x80\xff\xff\x81\x00\x00\x00\x00\x00" * 4000
        self.write("nested/pcm24.wav", wav_bytes([a, b], 3))
        self.write("pcm24.rf64", wav_bytes([a, b], 3, rf64=True))
        dsd = [rng.randbytes(8193), rng.randbytes(8193)]
        self.write("native.dsf", dsf_bytes(dsd, frames=8193 * 8 - 3, padding=0xAE))
        self.write("native.dff", dff_bytes(dsd))
        self.write("opaque.bin", (b"\x00generic archive file\xff" * 900))
        self.write("empty.bin", b"")

    def test_all_recipes_auto_selects_smallest_complete_archive(self):
        self.small_mixed_corpus()
        sources = self.source_map()
        originals = {name: path.read_bytes() for name, path in sources.items()}
        forced = {}
        for mode in ("raw", "native", "semantic", "hybrid", "auto"):
            with self.subTest(mode=mode):
                archive = self.root / f"{mode}.mva"
                report = self.pack(archive, mode=mode, allow_growth=True)
                self.assertEqual(report["status"], "created")
                self.assertTrue(report["verified"])
                self.assertEqual(report["archive_bytes"], archive.stat().st_size)
                self.assertEqual(report["archive_bytes"], min(report["candidate_bytes"].values()))
                checked = mv.verify(archive, compare_sources=sources, work_dir=self.work)
                self.assertTrue(checked["verified"])
                inspected = mv.inspect_archive(archive)
                self.assertFalse(inspected["content_verified"])
                self.assertEqual(inspected["original_bytes"], sum(map(len, originals.values())))
                self.assertEqual(inspected["files"][0]["path"], sorted(sources)[0])
                destination = self.root / f"restored-{mode}"
                restored = mv.unpack(archive, destination)
                self.assertEqual(restored["restored_files"], len(sources))
                recovered = {p.relative_to(destination).as_posix(): p.read_bytes()
                             for p in destination.rglob("*") if p.is_file()}
                self.assertEqual(recovered, originals)
                self.assertEqual({name: path.read_bytes() for name, path in sources.items()}, originals)
                if mode == "auto":
                    self.assertEqual(set(report["candidate_bytes"]), {"raw", "native", "semantic", "hybrid"})
                    self.assertLessEqual(report["archive_bytes"], min(forced.values()))
                else:
                    forced[mode] = report["archive_bytes"]
                self.assert_clean()

    def test_no_net_savings_skip_and_explicit_growth(self):
        path = self.write("tiny.bin", bytes(range(37)))
        archive = self.root / "tiny.mva"
        skipped = self.pack(archive, mode="raw")
        self.assertEqual(skipped["status"], "skipped_no_net_savings")
        self.assertLessEqual(skipped["net_saving_bytes"], 0)
        self.assertFalse(archive.exists())
        self.assertEqual(path.read_bytes(), bytes(range(37)))
        report = self.pack(archive, mode="raw", allow_growth=True)
        self.assertEqual(report["status"], "created")
        self.assertTrue(mv.verify(archive, work_dir=self.work)["verified"])
        self.assert_clean()

    def test_existing_archive_and_restore_destination_preserved(self):
        source = self.write("master.bin", b"master copy" * 9000)
        archive = self.root / "old.mva"
        archive.write_bytes(b"pre-existing valuable archive")
        with self.assertRaises(mv.ArchiveError):
            self.pack(archive)
        self.assertEqual(archive.read_bytes(), b"pre-existing valuable archive")
        with self.assertRaises(mv.ArchiveError):
            mv.pack([source], source, codecs="stdlib", work_dir=self.work)
        self.assertEqual(source.read_bytes(), b"master copy" * 9000)
        good = self.root / "good.mva"
        self.pack(good, mode="raw", allow_growth=True)
        destination = self.root / "existing"
        destination.mkdir()
        sentinel = destination / "valuable.txt"
        sentinel.write_bytes(b"original")
        with self.assertRaises(mv.ArchiveError):
            mv.unpack(good, destination)
        self.assertEqual(list(destination.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b"original")
        self.assert_clean()

    def test_commit_failure_cleans_staging_and_preserves_sources(self):
        source = self.write("master.bin", b"preserve me\x00" * 12_000)
        archive = self.root / "failed.mva"
        before = source.read_bytes()
        with patch.object(mv.os, "link", side_effect=OSError("injected publish failure")):
            with self.assertRaisesRegex(OSError, "injected"):
                self.pack(archive, mode="raw", allow_growth=True)
        self.assertFalse(archive.exists())
        self.assertEqual(source.read_bytes(), before)
        self.assert_clean()

    def test_destination_race_during_publish_is_not_overwritten(self):
        self.write("master.bin", b"zero" * 30_000)
        archive = self.root / "raced.mva"
        link = os.link
        def race(first, second):
            Path(second).write_bytes(b"arrived concurrently")
            return link(first, second)
        with patch.object(mv.os, "link", side_effect=race):
            with self.assertRaises(FileExistsError):
                self.pack(archive, mode="raw", allow_growth=True)
        self.assertEqual(archive.read_bytes(), b"arrived concurrently")
        self.assert_clean()

    def test_second_restore_failure_and_rename_failure_clean_staging(self):
        self.write("first.bin", b"first" * 20_000)
        self.write("second.bin", b"second" * 20_000)
        archive = self.root / "valid.mva"
        self.pack(archive, mode="raw", allow_growth=True)
        original_restore = mv._restore_file
        calls = 0
        def failed_restore(db, entry, target, codecs):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write_bytes(b"partial second output")
                raise OSError("injected second file write failure")
            return original_restore(db, entry, target, codecs)
        destination = self.root / "failed-restoration"
        with patch.object(mv, "_restore_file", side_effect=failed_restore):
            with self.assertRaisesRegex(OSError, "second file"):
                mv.unpack(archive, destination)
        self.assertEqual(calls, 2)
        self.assertFalse(destination.exists())
        self.assert_clean()
        rename = Path.rename
        def failed_rename(path, target):
            if path.name.startswith(".mastervault-restore-"):
                raise OSError("injected rename failure")
            return rename(path, target)
        with patch.object(Path, "rename", failed_rename):
            with self.assertRaisesRegex(OSError, "rename"):
                mv.unpack(archive, destination)
        self.assertFalse(destination.exists())
        self.assert_clean()
        self.assertTrue(mv.verify(archive, self.source_map(), work_dir=self.work)["verified"])

    def test_directory_walk_errors_are_fatal(self):
        self.write("visible.bin", b"visible" * 10000)
        archive = self.root / "incomplete.mva"
        def failed_walk(path, **kwargs):
            kwargs["onerror"](PermissionError("injected unreadable subdirectory"))
            return iter(())
        with patch.object(mv.os, "walk", side_effect=failed_walk):
            with self.assertRaisesRegex(PermissionError, "unreadable"):
                self.pack(archive)
        self.assertFalse(archive.exists())
        self.assert_clean()

    def test_source_mutation_during_pack_is_detected(self):
        source = self.write("mutable.bin", b"source snapshot" * 10_000)
        archive = self.root / "changed.mva"
        original = mv.Pool.file
        def mutate(pool, file_id, path, info, average, strategies):
            original(pool, file_id, path, info, average, strategies)
            with path.open("r+b") as handle:
                handle.write(b"X")
            item = path.stat()
            os.utime(path, ns=(item.st_atime_ns, item.st_mtime_ns + 100_000_000))
        with patch.object(mv.Pool, "file", mutate):
            with self.assertRaisesRegex(mv.ArchiveError, "Source changed"):
                self.pack(archive, mode="raw", allow_growth=True)
        self.assertFalse(archive.exists())
        self.assertEqual(source.read_bytes()[0:1], b"X")
        self.assert_clean()

    def test_verify_compares_current_source_bytes_not_only_length(self):
        source = self.write("master.bin", b"original master" * 10_000)
        archive = self.root / "source-compare.mva"
        self.pack(archive, mode="raw", allow_growth=True)
        sources = self.source_map()
        self.assertTrue(mv.verify(archive, sources, work_dir=self.work)["verified"])
        item = source.stat()
        with source.open("r+b") as handle:
            handle.seek(17)
            handle.write(b"X")
        os.utime(source, ns=(item.st_atime_ns, item.st_mtime_ns))
        with self.assertRaisesRegex(mv.ArchiveError, "differ"):
            mv.verify(archive, sources, work_dir=self.work)
        with self.assertRaisesRegex(mv.ArchiveError, "differ"):
            mv.verify(archive, {}, work_dir=self.work)
        self.assertTrue(mv.verify(archive, work_dir=self.work)["verified"])
        self.assert_clean()


if __name__ == "__main__":
    unittest.main()
