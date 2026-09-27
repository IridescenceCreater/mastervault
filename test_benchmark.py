"""Tests for benchmark evidence, including rejecting a lossy reference result."""
from __future__ import annotations

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import benchmark
from fixtures import dff_bytes, dsf_bytes, pcm_wav
import formats


class FixtureAndReferenceTests(unittest.TestCase):
    def test_dsf_padding_tail_and_bit_order_reconstruct_exactly(self):
        channels = [b"\xA9\x38\x41\xFF\xB7", b"\x13\x57\x9B\xCF\xE5"]
        with tempfile.TemporaryDirectory() as directory:
            for lsb in (False, True):
                path = Path(directory) / f"order-{lsb}.dsf"
                original = dsf_bytes(channels, lsb=lsb, sample_count=35, padding=0xA7,
                                     metadata=b"ID3\x04\0\0\0\0\0\0")
                path.write_bytes(original)
                info = formats.probe(path)
                self.assertIsNotNone(info)
                self.assertEqual(info["frames"], 35)
                output = io.BytesIO(bytes(len(original)))
                for offset, length in formats.raw_spans(info, len(original)):
                    output.seek(offset)
                    output.write(original[offset:offset + length])
                for index, channel in enumerate(channels):
                    normalized = b"".join(formats.iter_channel(path, info, index, chunk_bytes=3))
                    self.assertEqual(normalized, channel)
                    formats.write_channel(output, info, index, 0, normalized)
                self.assertEqual(output.getvalue(), original)

    def test_dsf_and_dff_expose_identical_channels(self):
        channels = [bytes(range(64)), bytes(reversed(range(64)))]
        with tempfile.TemporaryDirectory() as directory:
            for name, original in (("lsb.dsf", dsf_bytes(channels)),
                                   ("msb.dsf", dsf_bytes(channels, lsb=False)),
                                   ("wrapped.dff", dff_bytes(channels, extra_chunks=True))):
                path = Path(directory) / name
                path.write_bytes(original)
                info = formats.probe(path)
                self.assertIsNotNone(info)
                self.assertEqual([b"".join(formats.iter_channel(path, info, i)) for i in range(2)], channels)

    def test_dirty_valid24_keeps_all_32_container_bits(self):
        values = [[-2147483647, 2147483646], [-257, 259], [0x1234567F, -0x12345671]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dirty.wav"
            path.write_bytes(pcm_wav(values, 4, 192000, valid_bits=24))
            info = formats.probe(path)
            self.assertEqual(info["valid_bits"], 24)
            for channel in range(2):
                actual = b"".join(formats.iter_channel(path, info, channel))
                expected = b"".join(frame[channel].to_bytes(4, "little", signed=True) for frame in values)
                self.assertEqual(actual, expected)

    def test_zip_reference_counts_complete_archive_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            source.write_bytes(pcm_wav([[i, -i] for i in range(200)], 3, extra_chunks=True))
            result = benchmark.zip_reference([source], root / "reference")
            self.assertTrue(result["verified"])
            self.assertEqual(result["archive_bytes"], Path(result["archive_path"]).stat().st_size)
            self.assertEqual((root / "reference/restored/source.wav").read_bytes(), source.read_bytes())

    def test_lossy_reference_output_is_never_credited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.dsf"
            original = dsf_bytes([b"\x95\xAA", b"\x67\xDB"], padding=0xD3)
            source.write_bytes(original)

            def fake_tool(command):
                destination = Path(command[-1])
                if command[0] == "fake_encoder":
                    destination.write_bytes(b"fake reference codec stream")
                else:
                    # Simulate a reference codec preserving audio but replacing
                    # the last non-audio padding byte. Process exit is success.
                    destination.write_bytes(original[:-1] + b"\0")
                return {"command": command, "returncode": 0, "seconds": 0,
                        "stdout": "", "stderr": ""}

            with patch("benchmark.run_tool", side_effect=fake_tool):
                result = benchmark.wavpack_reference([source], root / "reference", "fake_encoder", "fake_decoder")
            self.assertFalse(result["verified"])
            self.assertIsNone(result["archive_bytes"])
            self.assertEqual(result["files"][0]["status"], "original_file_bytes_changed")
            self.assertFalse((root / "reference/whole_file_wavpack.zip").exists())

    def test_flac_envelope_preserves_unknown_chunks_and_signed_samples(self):
        ffmpeg = benchmark.choose_tool(None, "ffmpeg")
        if ffmpeg is None:
            self.skipTest("Optional FFmpeg reference encoder not available")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            source.write_bytes(pcm_wav([[i * 1701, -i * 1301] for i in range(1000)], 3, 96000, extra_chunks=True))
            result = benchmark.flac_reference([source], root / "reference", ffmpeg)
            self.assertTrue(result["verified"], result)
            self.assertEqual((root / "reference/restored/source.wav").read_bytes(), source.read_bytes())

    def test_official_wavpack_whole_dsd_wrapper_roundtrip(self):
        encoder = benchmark.choose_tool(None, "wavpack")
        decoder = benchmark.choose_tool(None, "wvunpack")
        if not encoder or not decoder:
            self.skipTest("Optional official WavPack reference binaries not available")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.dsf"
            source.write_bytes(dsf_bytes([b"\x96\x69" * 4096, b"\xAA\x55" * 4096]))
            result = benchmark.wavpack_reference([source], root / "reference", encoder, decoder)
            self.assertTrue(result["verified"], result)
            self.assertEqual((root / "reference/restored/source.dsf").read_bytes(), source.read_bytes())

    def test_flac_32bit_reference_keeps_dirty_lsb_and_extreme_samples(self):
        ffmpeg = benchmark.choose_tool(None, "ffmpeg")
        if ffmpeg is None:
            self.skipTest("Optional FFmpeg reference encoder not available")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "dirty32.wav"
            frames = [[-2147483647, 2147483646], [-257, 259], [0x1234567F, -0x12345671]] * 257
            source.write_bytes(pcm_wav(frames, 4, 192000, valid_bits=24, extra_chunks=True))
            result = benchmark.flac_reference([source], root / "reference", ffmpeg)
            self.assertTrue(result["verified"], result)
            self.assertEqual((root / "reference/restored/dirty32.wav").read_bytes(), source.read_bytes())

    def test_wavpack_raw_pcm_wrapper_recovers_dirty_valid24(self):
        encoder = benchmark.choose_tool(None, "wavpack")
        decoder = benchmark.choose_tool(None, "wvunpack")
        if not encoder or not decoder:
            self.skipTest("Optional official WavPack reference binaries not available")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "dirty32.wav"
            frames = [[-2147483647, 2147483646], [-257, 259], [0x1234567F, -0x12345671]] * 257
            source.write_bytes(pcm_wav(frames, 4, 192000, valid_bits=24, extra_chunks=True))
            result = benchmark.wavpack_reference([source], root / "reference", encoder, decoder)
            self.assertTrue(result["verified"], result)
            record = result["files"][0]
            self.assertEqual(record["reference_mode"], "raw_pcm_plus_exact_wrapper")
            self.assertEqual(len(record["whole_file_attempts"]), 2)
            self.assertIn("--force-even-byte-depth", record["whole_file_attempts"][1]["command"])
            self.assertEqual((root / "reference/restored/dirty32.wav").read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
