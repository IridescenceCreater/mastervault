"""Independent hostile-archive and failure-injection tests.

Tests mutate the SQLite archive itself, including recomputing attacker-controlled
checksums, so success is not just rejection by one outer digest.
"""

from __future__ import annotations

import gc
from contextlib import closing
from pathlib import Path
import random
import shutil
import sqlite3
import tempfile
import tracemalloc
import unittest
from unittest import mock

import mastervault as mv
from codecs_layer import CodecError


class ArchiveAttackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_temp = tempfile.TemporaryDirectory(prefix="mastervault-attack-fixture-")
        cls.root = Path(cls.root_temp.name)
        cls.sources = cls.root / "input"
        cls.sources.mkdir()
        cls.content = {"a.bin": b"mastervault\0\xff" * 3000,
                       "b.bin": random.Random(828191).randbytes(35000)}
        for name, data in cls.content.items():
            (cls.sources / name).write_bytes(data)
        cls.base = cls.root / "base.mva"
        mv.pack([cls.sources], cls.base, average=16384, mode="raw", allow_growth=True, codecs="stdlib")

    @classmethod
    def tearDownClass(cls):
        cls.root_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mastervault-attack-")
        self.folder = Path(self.temp.name)
        self.counter = 0

    def tearDown(self):
        self.temp.cleanup()

    def mutate(self, commands):
        self.counter += 1
        archive = self.folder / f"attack-{self.counter}.mva"
        shutil.copyfile(self.base, archive)
        with closing(sqlite3.connect(archive)) as db:
            with db:
                for command, parameters in commands:
                    db.execute(command, parameters)
        return archive

    def rejects(self, archive):
        with self.assertRaises((mv.ArchiveError, CodecError)):
            mv.verify(archive, work_dir=self.folder)

    def test_base_archive_roundtrip_is_valid(self):
        self.assertTrue(mv.verify(self.base, work_dir=self.folder)["verified"])
        output = self.folder / "restored"
        mv.unpack(self.base, output)
        for name, data in self.content.items():
            self.assertEqual((output / "input" / name).read_bytes(), data)

    def test_unsafe_names_blocked_before_any_restore(self):
        names = ["../outside", "/absolute", "C:/absolute", "x\\y", "a//b", "a/./b", "a/../b",
                 "stream:ads", "NUL", "NUL.txt", "CON .txt", "COM1.wav", "LPT².wav", "CONIN$",
                 "trailing.", "trailing ", "bad\0name", "a/", "\ud800"]
        for name in names:
            with self.subTest(name=repr(name)):
                if "\ud800" in name:
                    # SQLite refuses lone surrogates before writing. Test the
                    # archive's own portable-name guard directly as well.
                    with self.assertRaises(mv.ArchiveError):
                        mv.safe_name(name)
                    continue
                archive = self.mutate([("UPDATE files SET path=? WHERE id=1", (name,))])
                with mock.patch.object(mv, "_restore_file", side_effect=AssertionError("restore started before path validation")):
                    self.rejects(archive)

    def test_case_unicode_and_file_directory_conflicts(self):
        for first, second in (("A.bin", "a.BIN"), ("é.bin", "e\u0301.bin"), ("folder", "folder/file.bin")):
            archive = self.mutate([("UPDATE files SET path=? WHERE id=1", (first,)),
                                   ("UPDATE files SET path=? WHERE id=2", (second,))])
            self.rejects(archive)

    def test_view_trigger_and_schema_drift_rejected(self):
        for statement in ("CREATE VIEW unexpected AS SELECT * FROM objects",
                          "CREATE TRIGGER unexpected AFTER INSERT ON meta BEGIN SELECT 1; END",
                          "CREATE TABLE unexpected(x)", "DROP INDEX object_hash"):
            with self.subTest(statement=statement):
                self.rejects(self.mutate([(statement, ())]))

    def test_incomplete_or_unknown_archive_meta_rejected(self):
        attacks = [("UPDATE meta SET value='no' WHERE key='complete'", ()),
                   ("UPDATE meta SET value='future-format' WHERE key='version'", ()),
                   ("DELETE FROM meta WHERE key='complete'", ()),
                   ("INSERT INTO meta VALUES('unexpected','x')", ())]
        for attack in attacks:
            self.rejects(self.mutate([attack]))

    def test_malformed_settings_and_duplicate_json_keys(self):
        settings = ["null", "[]", "{}", '{"strategy":"raw","strategy":"raw"}',
                    '{"strategy":"raw","average":true,"external_codecs_are_lossless_dependencies":true}',
                    '{"strategy":"raw","average":16385,"external_codecs_are_lossless_dependencies":true}',
                    '{"strategy":"raw","average":16384,"external_codecs_are_lossless_dependencies":1}',
                    "[" * 5000 + "]" * 5000]
        for value in settings:
            self.rejects(self.mutate([("UPDATE meta SET value=? WHERE key='settings'", (value,))]))

    def test_negative_and_oversized_file_lengths(self):
        for size in (-1, mv.MAX_TOTAL + 1):
            self.rejects(self.mutate([("UPDATE files SET size=? WHERE id=1", (size,))]))

    def test_foreign_or_missing_references(self):
        for sql in ("UPDATE pieces SET object_id=-1", "UPDATE pieces SET object_id=9223372036854775807",
                    "UPDATE recipes SET file_id=999999 WHERE id=1", "DELETE FROM objects",
                    "UPDATE pieces SET stream_id=999999 WHERE stream_id=(SELECT min(id) FROM streams)",
                    "UPDATE streams SET recipe_id=999999"):
            with self.subTest(sql=sql):
                self.rejects(self.mutate([(sql, ())]))

    def test_noncontiguous_piece_sequence_and_sizes(self):
        for sql in ("UPDATE pieces SET ordinal=ordinal+100", "UPDATE pieces SET size=0",
                    "UPDATE pieces SET size=4194305", "UPDATE pieces SET size=size-1",
                    "DELETE FROM pieces WHERE stream_id=(SELECT min(id) FROM streams)"):
            with self.subTest(sql=sql):
                self.rejects(self.mutate([(sql, ())]))

    def test_overlaps_holes_and_false_channel_layout(self):
        for sql in ("UPDATE streams SET offset=1 WHERE id=1", "UPDATE streams SET size=size+1 WHERE id=1",
                    "UPDATE streams SET role='channel' WHERE id=1", "UPDATE streams SET channel=0 WHERE id=1"):
            with self.subTest(sql=sql):
                self.rejects(self.mutate([(sql, ())]))

    def test_extra_unreferenced_object_rejected(self):
        sql = ("INSERT INTO objects(bucket,sha256,size,codec,width,encoded_sha256,data) "
               "SELECT bucket,sha256,size,codec,width,encoded_sha256,data FROM objects LIMIT 1")
        self.rejects(self.mutate([(sql, ())]))

    def test_raw_piece_cannot_smuggle_transform_params(self):
        for value in ("true", "[]", '{"anchor":0}', '{"a":1,"a":2}'):
            self.rejects(self.mutate([("UPDATE pieces SET params=?", (value,))]))
        self.rejects(self.mutate([("UPDATE pieces SET transform='pcm'", ())]))

    def test_object_content_and_encoded_hash_validation(self):
        self.rejects(self.mutate([("UPDATE objects SET encoded_sha256=?", ("0" * 64,))]))
        self.rejects(self.mutate([("UPDATE objects SET sha256=?", ("0" * 64,))]))
        # The attacker may recompute the encoded checksum; codec/decoded hash
        # checks still have to reject the replacement.
        self.rejects(self.mutate([("UPDATE objects SET data=?,encoded_sha256=?", (b"corrupt", mv.sha(b"corrupt")))]))
        self.rejects(self.mutate([("UPDATE files SET sha256=?", ("0" * 64,))]))

    def test_object_schema_fields_rejected(self):
        for sql in ("UPDATE objects SET codec='unknown'", "UPDATE objects SET width=0",
                    "UPDATE objects SET size=-1", "UPDATE objects SET size=8388609",
                    "UPDATE objects SET data='not-a-blob'", "UPDATE objects SET sha256='bad'"):
            with self.subTest(sql=sql):
                self.rejects(self.mutate([(sql, ())]))

    def test_hidden_meta_payload_rejected_before_large_python_allocation(self):
        # Small enough to run safely, large enough to expose fetch-before-limit.
        archive = self.mutate([("INSERT INTO meta VALUES('extra',?)", ("x" * (4 * 1024 * 1024),))])
        gc.collect()
        tracemalloc.start()
        try:
            self.rejects(archive)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024, "unbounded meta text was fetched before its schema/length checks")

    def test_digest_text_rejected_before_large_python_allocation(self):
        archive = self.mutate([("UPDATE files SET sha256=?", ("x" * (4 * 1024 * 1024),))])
        gc.collect()
        tracemalloc.start()
        try:
            self.rejects(archive)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 1024 * 1024, "digest text bypassed the file metadata memory preflight")

    def test_hash_bucket_collisions_still_compare_real_object_bytes(self):
        sources = self.folder / "collisions"
        sources.mkdir()
        rng = random.Random(192738)
        first, second = rng.randbytes(32000), rng.randbytes(32000)
        (sources / "a").write_bytes(first)
        (sources / "b").write_bytes(second)
        archive = self.folder / "collision.mva"
        with mock.patch.object(mv, "bucket_hash", return_value="constant-test-index"):
            mv.pack([sources], archive, mode="raw", average=16384, codecs="stdlib", allow_growth=True)
        output = self.folder / "collision-out"
        mv.unpack(archive, output)
        self.assertEqual((output / "collisions" / "a").read_bytes(), first)
        self.assertEqual((output / "collisions" / "b").read_bytes(), second)

    def test_existing_restore_directory_is_preserved(self):
        output = self.folder / "occupied"
        output.mkdir()
        (output / "marker").write_bytes(b"keep")
        with self.assertRaises(mv.ArchiveError):
            mv.unpack(self.base, output)
        self.assertEqual((output / "marker").read_bytes(), b"keep")

    def test_failed_second_restore_does_not_publish_partial_output(self):
        output = self.folder / "restore"
        actual = mv._restore_file
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected write failure")
            return actual(*args, **kwargs)

        with mock.patch.object(mv, "_restore_file", side_effect=fail_second):
            with self.assertRaises(OSError):
                mv.unpack(self.base, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.folder.glob(".mastervault-restore-*")), [])

    def test_publish_failure_preserves_sources_and_existing_data(self):
        destination = self.folder / "cannot-publish.mva"
        with mock.patch.object(mv.os, "link", side_effect=OSError("injected commit failure")):
            with self.assertRaises(OSError):
                mv.pack([self.sources], destination, mode="raw", average=16384, codecs="stdlib", allow_growth=True)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.folder.glob(".mastervault-*")), [])
        for name, data in self.content.items():
            self.assertEqual((self.sources / name).read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
