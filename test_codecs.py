"""Object-codec attacks, including forced tests of available native codecs."""

from __future__ import annotations

import lzma
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zlib

import codecs_layer
from codecs_layer import Codecs, CodecError, MAX_OBJECT, _bounded_process, _dsf


class GeneralCodecTests(unittest.TestCase):
    def setUp(self):
        self.codecs = Codecs(external=False)

    def test_builtin_selection_byte_exact_including_empty(self):
        rng = random.Random(72115)
        for data in (b"", b"a", b"\0" * 20000, bytes(range(256)) * 200, rng.randbytes(32000)):
            codec, width, encoded = self.codecs.encode(data)
            self.assertLessEqual(len(encoded), len(data))
            self.assertEqual(self.codecs.decode(encoded, codec, len(data), width), data)
        self.assertEqual(self.codecs.encode(b""), ("raw", 1, b""))

    def test_encoded_inputs_must_be_bytes(self):
        for data in ([], bytearray(b"abc"), memoryview(b"abc"), "abc", None):
            with self.subTest(type=type(data)), self.assertRaises(CodecError):
                self.codecs.encode(data)
            with self.subTest(decode_type=type(data)), self.assertRaises(CodecError):
                self.codecs.decode(data, "raw", 3)

    def test_encode_kind_width_and_alignment_validation(self):
        for kind, width, data in (("unknown", 1, b"abc"), (True, 1, b"abc"),
                                  ("raw", True, b"abc"), ("raw", 2, b"abc"),
                                  ("dsd", 3, b"abc"), ("pcm", True, b"abc"),
                                  ("pcm", 1, b"abc"), ("pcm", 3, b"ab"),
                                  ("pcm", 4.0, b"abcd")):
            with self.subTest(kind=kind, width=width), self.assertRaises(CodecError):
                self.codecs.encode(data, kind, width)

    def test_decode_shape_and_codec_validation(self):
        for size in (True, False, -1, MAX_OBJECT + 1, 1.0, "3", None):
            with self.subTest(size=size), self.assertRaises(CodecError):
                self.codecs.decode(b"abc", "raw", size)
        for width in (True, False, -1, 0, 5, 1.0, "1", None):
            with self.subTest(width=width), self.assertRaises(CodecError):
                self.codecs.decode(b"abc", "raw", 3, width)
        for codec in ("unknown", "FLAC", True, 7, None, []):
            with self.subTest(codec=codec), self.assertRaises(CodecError):
                self.codecs.decode(b"abc", codec, 3)
        for size in (0, 2, 4):
            with self.assertRaises(CodecError):
                self.codecs.decode(b"abc", "raw", size)

    def test_encoded_size_limit(self):
        with self.assertRaises(CodecError):
            self.codecs.encode(b"\0" * (MAX_OBJECT + 1))
        with self.assertRaises(CodecError):
            self.codecs.decode(b"\0" * (MAX_OBJECT + 65537), "raw", 0)

    def test_zlib_and_lzma_complete_streams_only(self):
        source = (b"sample-\0\xFF" * 8000)
        for name, encoder in (("zlib", zlib.compress), ("lzma", lzma.compress)):
            encoded = encoder(source)
            self.assertEqual(self.codecs.decode(encoded, name, len(source)), source)
            self.assertEqual(self.codecs.decode(encoder(b""), name, 0), b"")
            attacks = (b"", encoded[:-1], encoded[:5], encoded + b"garbage",
                       encoded + encoded, encoded + encoder(b""))
            for bad in attacks:
                with self.subTest(codec=name, length=len(bad)), self.assertRaises(CodecError):
                    self.codecs.decode(bad, name, len(source))
            for size in (0, 7, len(source) - 1, len(source) + 1):
                with self.subTest(codec=name, declared_size=size), self.assertRaises(CodecError):
                    self.codecs.decode(encoded, name, size)

    def test_bombs_cannot_ignore_declared_small_output(self):
        data = b"\0" * (2 * 1024 * 1024)
        for name, encoder in (("zlib", zlib.compress), ("lzma", lzma.compress)):
            with self.subTest(codec=name), self.assertRaises(CodecError):
                self.codecs.decode(encoder(data), name, 8)

    def test_lzma_dictionary_memory_request_rejected(self):
        # LZMA-alone has no header CRC; increase its decoder dictionary request
        # without allocating that dictionary while constructing this attack.
        encoded = bytearray(lzma.compress(b"A" * 4096, format=lzma.FORMAT_ALONE, preset=0))
        encoded[1:5] = (128 * 1024 * 1024).to_bytes(4, "little")
        with self.assertRaises(CodecError):
            self.codecs.decode(bytes(encoded), "lzma", 4096)

    def test_external_encoder_errors_fall_back_exactly(self):
        data = bytes(range(256)) * 16
        for failure in (CodecError("encoder rejected"), OSError("tool unavailable"),
                        subprocess.TimeoutExpired("codec", 0.1),
                        subprocess.CalledProcessError(2, "codec")):
            codecs = Codecs()
            codecs.ffmpeg = "placeholder-for-mocked-command"
            with mock.patch.object(codecs_layer, "_bounded_process", side_effect=failure):
                codec, width, encoded = codecs.encode(data, "pcm", 4)
            self.assertNotEqual(codec, "flac")
            self.assertEqual(codecs.rejected_external_candidates, 1)
            self.assertEqual(codecs.decode(encoded, codec, len(data), width), data)

    def test_same_length_silent_external_loss_is_rejected(self):
        data = bytes(range(256)) * 16
        codecs = Codecs()
        codecs.ffmpeg = "placeholder-for-mocked-command"
        # Tiny native candidate wins, but its decoder silently drops all bits.
        with mock.patch.object(codecs_layer, "_bounded_process", side_effect=[b"x", bytes(len(data))]):
            codec, width, encoded = codecs.encode(data, "pcm", 4)
        self.assertNotEqual(codec, "flac")
        self.assertEqual(codecs.rejected_external_candidates, 1)
        self.assertEqual(codecs.decode(encoded, codec, len(data), width), data)

    def test_missing_native_decoder_is_explicit(self):
        self.codecs.ffmpeg = None
        self.codecs.wvunpack = None
        for codec, size, width in (("flac", 4, 4), ("wavpack-dsd", 1, 1)):
            with self.assertRaises(CodecError):
                self.codecs.decode(b"garbage", codec, size, width)


class ProcessBoundaryTests(unittest.TestCase):
    def test_process_output_limit_and_nonzero_exit(self):
        with self.assertRaises(CodecError):
            _bounded_process([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'x'*1025)"], 1024)
        with self.assertRaises(CodecError):
            _bounded_process([sys.executable, "-c", "raise SystemExit(9)"], 1024)
        self.assertEqual(_bounded_process([sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'abc')"], 3), b"abc")

    def test_stalled_process_deadline(self):
        started = time.monotonic()
        with self.assertRaises(CodecError):
            _bounded_process([sys.executable, "-c", "import time;time.sleep(5)"], 16, timeout=0.1)
        self.assertLess(time.monotonic() - started, 3)


class DecodeCacheTests(unittest.TestCase):
    def setUp(self):
        self.codecs = Codecs(external=False)

    def test_full_encoded_bytes_are_isolated_even_at_same_length(self):
        # Same codec, size, width and long prefix; only the final encoded byte
        # differs. A cache keyed by a prefix/shape cannot pass this test.
        first = b"identical-prefix-" * 100 + b"A"
        second = first[:-1] + b"B"
        self.assertEqual(self.codecs.decode(first, "raw", len(first)), first)
        self.assertEqual(self.codecs.decode(second, "raw", len(second)), second)
        self.assertEqual(self.codecs.decode(first, "raw", len(first)), first)
        self.assertEqual(len(self.codecs._decoded_cache), 2)

    def test_same_encoded_bytes_under_different_codecs_do_not_alias(self):
        decoded = b"a" * 11
        encoded = zlib.compress(decoded)
        self.assertEqual(len(encoded), len(decoded))
        self.assertNotEqual(encoded, decoded)
        self.assertEqual(self.codecs.decode(encoded, "raw", len(decoded)), encoded)
        self.assertEqual(self.codecs.decode(encoded, "zlib", len(decoded)), decoded)
        self.assertEqual(self.codecs.decode(encoded, "raw", len(decoded)), encoded)

    def test_expected_length_and_strict_types_checked_on_warm_cache(self):
        encoded = zlib.compress(b"cache-value")
        self.assertEqual(self.codecs.decode(encoded, "zlib", 11), b"cache-value")
        for wrong_length in (10, 12):
            with self.assertRaises(CodecError):
                self.codecs.decode(encoded, "zlib", wrong_length)
        self.assertEqual(self.codecs.decode(b"x", "raw", 1, 1), b"x")
        # bool and int compare equal in Python tuple keys; validation must run
        # before a cache lookup or these will alias the valid entry.
        for size, width in ((True, 1), (1, True), (1.0, 1), (1, 1.0)):
            with self.assertRaises(CodecError):
                self.codecs.decode(b"x", "raw", size, width)
        for wrong_type in (bytearray(b"x"), memoryview(b"x")):
            with self.assertRaises(CodecError):
                self.codecs.decode(wrong_type, "raw", 1, 1)

    def test_native_width_is_part_of_cache_key(self):
        self.codecs.ffmpeg = "mocked-ffmpeg"
        with mock.patch.object(codecs_layer, "_bounded_process", side_effect=[b"wide", b"thin"]) as run:
            self.assertEqual(self.codecs.decode(b"encoded", "flac", 4, 4), b"wide")
            self.assertEqual(self.codecs.decode(b"encoded", "flac", 4, 2), b"thin")
            self.assertEqual(self.codecs.decode(b"encoded", "flac", 4, 4), b"wide")
            self.assertEqual(self.codecs.decode(b"encoded", "flac", 4, 2), b"thin")
            self.assertEqual(run.call_count, 2)

    def test_missing_native_dependencies_not_hidden_by_warm_cache(self):
        for codec, width, attribute in (("flac", 4, "ffmpeg"), ("wavpack-dsd", 1, "wvunpack")):
            codecs = Codecs(external=False)
            setattr(codecs, attribute, "mocked-decoder")
            with mock.patch.object(codecs_layer, "_bounded_process", return_value=b"data") as run:
                self.assertEqual(codecs.decode(b"encoded", codec, 4, width), b"data")
                setattr(codecs, attribute, None)
                with self.assertRaises(CodecError):
                    codecs.decode(b"encoded", codec, 4, width)
                self.assertEqual(run.call_count, 1)

    def test_encoded_and_decoded_bytes_both_count_toward_budget(self):
        self.codecs.wvunpack = "mocked-decoder"
        self.codecs._decoded_cache_limit = 400
        first, second = b"A" * 200, b"B" * 200
        with mock.patch.object(codecs_layer, "_bounded_process", return_value=b"data") as run:
            self.codecs.decode(first, "wavpack-dsd", 4)
            self.codecs.decode(second, "wavpack-dsd", 4)
            self.assertEqual(len(self.codecs._decoded_cache), 1)
            self.assertEqual(self.codecs._decoded_cache_bytes, 200 + 4 + 128)
            self.assertLessEqual(self.codecs._decoded_cache_bytes, self.codecs._decoded_cache_limit)
            # The first entry was evicted; re-reading it must decode again.
            self.codecs.decode(first, "wavpack-dsd", 4)
            self.assertEqual(run.call_count, 3)

    def test_cache_hit_updates_lru_and_evicts_least_recent_entry(self):
        self.codecs.wvunpack = "mocked-decoder"
        self.codecs._decoded_cache_limit = 2 * (4 + 4 + 128)
        with mock.patch.object(codecs_layer, "_bounded_process", return_value=b"data") as run:
            for encoded in (b"aaaa", b"bbbb", b"aaaa", b"cccc", b"aaaa", b"bbbb"):
                self.assertEqual(self.codecs.decode(encoded, "wavpack-dsd", 4), b"data")
            # A, B, C and the evicted B decode; both later A references hit.
            self.assertEqual(run.call_count, 4)
            self.assertEqual([key[3] for key in self.codecs._decoded_cache], [b"aaaa", b"bbbb"])
            self.assertEqual(self.codecs._decoded_cache_bytes, self.codecs._decoded_cache_limit)

    def test_oversized_entry_is_not_cached_and_does_not_flush_small_entry(self):
        self.codecs.wvunpack = "mocked-decoder"
        self.codecs._decoded_cache_limit = 256
        self.assertEqual(self.codecs.decode(b"tiny", "raw", 4), b"tiny")
        with mock.patch.object(codecs_layer, "_bounded_process", return_value=b"d" * 256) as run:
            for _ in range(2):
                self.assertEqual(self.codecs.decode(b"e" * 100, "wavpack-dsd", 256), b"d" * 256)
            self.assertEqual(run.call_count, 2)
        self.assertEqual(len(self.codecs._decoded_cache), 1)
        self.assertEqual(next(iter(self.codecs._decoded_cache))[3], b"tiny")
        self.assertEqual(self.codecs._decoded_cache_bytes, 4 + 4 + 128)

    def test_failed_or_wrong_length_decode_never_populates_cache(self):
        self.codecs.wvunpack = "mocked-decoder"
        with mock.patch.object(codecs_layer, "_bounded_process", side_effect=[b"bad", CodecError("failed"), b"good"]) as run:
            for _ in range(2):
                with self.assertRaises(CodecError):
                    self.codecs.decode(b"encoded", "wavpack-dsd", 4)
                self.assertFalse(self.codecs._decoded_cache)
                self.assertEqual(self.codecs._decoded_cache_bytes, 0)
            self.assertEqual(self.codecs.decode(b"encoded", "wavpack-dsd", 4), b"good")
            self.assertEqual(self.codecs.decode(b"encoded", "wavpack-dsd", 4), b"good")
            self.assertEqual(run.call_count, 3)


class NativeCodecTests(unittest.TestCase):
    def setUp(self):
        self.codecs = Codecs()

    def _native_flac(self, data, width):
        if not self.codecs.ffmpeg:
            self.skipTest("FFmpeg unavailable: native FLAC test not run")
        with tempfile.TemporaryDirectory(prefix="mastervault-test-flac-") as tmp:
            source = Path(tmp) / "samples.raw"
            source.write_bytes(data)
            fmt = f"s{width * 8}le"
            # Exercise native bit depth regardless of whether FLAC beats the
            # generic candidates on this small test object.
            command = [self.codecs.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                       "-f", fmt, "-ar", "48000", "-ac", "1", "-i", str(source),
                       "-map_metadata", "-1", "-flags", "+bitexact", "-c:a", "flac",
                       "-strict", "experimental", "-bits_per_raw_sample", str(width * 8),
                       "-compression_level", "8", "-metadata_header_padding", "0",
                       "-f", "flac", "pipe:1"]
            return _bounded_process(command, len(data) + 65536)

    def _native_wavpack(self, data):
        if not self.codecs.wavpack or not self.codecs.wvunpack:
            self.skipTest("WavPack pair unavailable: native DSD test not run")
        with tempfile.TemporaryDirectory(prefix="mastervault-test-wavpack-") as tmp:
            source, destination = Path(tmp) / "source.dsf", Path(tmp) / "compressed.wv"
            source.write_bytes(_dsf(data))
            result = subprocess.run([self.codecs.wavpack, "-q", "-hh", "--no-threads", str(source), str(destination)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            return destination.read_bytes()

    def test_flac_16_24_32_extremes_and_nonzero_low_bits(self):
        rng = random.Random(19751)
        for width in (2, 3, 4):
            bits = width * 8
            values = [-(1 << (bits - 1)), (1 << (bits - 1)) - 1, 1, -1, 255, -257, 257]
            values += [rng.randint(-(1 << (bits - 1)), (1 << (bits - 1)) - 1) for _ in range(5000)]
            data = b"".join(x.to_bytes(width, "little", signed=True) for x in values)
            with self.subTest(width=width):
                encoded = self._native_flac(data, width)
                self.assertEqual(self.codecs.decode(encoded, "flac", len(data), width), data)

    def test_flac_32bit_low_byte_alone_is_preserved(self):
        # This fails with the usual 24-bit default for FFmpeg's FLAC encoder.
        values = [(i % 255) - 127 for i in range(12000)]
        data = b"".join(struct.pack("<i", x) for x in values)
        encoded = self._native_flac(data, 4)
        self.assertEqual(self.codecs.decode(encoded, "flac", len(data), 4), data)
        with self.assertRaises(CodecError):
            self.codecs.decode(encoded, "flac", 4, 4)

    def test_wavpack_dsd_raw_is_msb_first_and_byte_exact(self):
        rng = random.Random(95126)
        for length in (1, 7, 4097, 65537):
            data = bytes((0x80,)) if length == 1 else rng.randbytes(length - 1) + b"\x81"
            with self.subTest(length=length):
                encoded = self._native_wavpack(data)
                # --raw must restore packed DSD bytes, never decoded PCM.
                self.assertEqual(self.codecs.decode(encoded, "wavpack-dsd", len(data), 1), data)

    def test_wavpack_single_bit_and_last_bit_preserved(self):
        data = bytearray(b"\x55" * 4097)
        data[0] ^= 0x80
        data[129] ^= 0x04
        data[-1] ^= 0x01
        encoded = self._native_wavpack(bytes(data))
        self.assertEqual(self.codecs.decode(encoded, "wavpack-dsd", len(data), 1), bytes(data))
        with self.assertRaises(CodecError):
            self.codecs.decode(encoded, "wavpack-dsd", 3, 1)

    def test_real_native_repeated_decode_launches_one_process_per_object(self):
        pcm = b"".join(struct.pack("<i", (i % 255) - 127) for i in range(1000))
        dsd = random.Random(37911).randbytes(4097)
        candidates = ((self._native_flac(pcm, 4), "flac", 4, pcm),
                      (self._native_wavpack(dsd), "wavpack-dsd", 1, dsd))
        native_decode = codecs_layer._bounded_process
        for encoded, codec, width, data in candidates:
            with self.subTest(codec=codec):
                with mock.patch.object(codecs_layer, "_bounded_process", wraps=native_decode) as run:
                    for _ in range(3):
                        self.assertEqual(self.codecs.decode(encoded, codec, len(data), width), data)
                    self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
