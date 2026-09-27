"""Adversarial format, channel isolation and byte-for-byte reconstruction tests."""

from __future__ import annotations

import io
import json
import os
import random
import struct
import tempfile
import unittest
from pathlib import Path

import formats


def wav_bytes(channel_data: list[bytes], width: int = 3, rate: int = 96_000,
              valid_bits: int | None = None, rf64: bool = False) -> bytes:
    channels = len(channel_data)
    assert channel_data and len({len(x) for x in channel_data}) == 1
    assert len(channel_data[0]) % width == 0
    frames = len(channel_data[0]) // width
    raw = b"".join(d[i:i + width] for i in range(0, frames * width, width) for d in channel_data)
    fmt = struct.pack("<HHIIHH", 0xFFFE if valid_bits is not None else 1,
                      channels, rate, rate * channels * width, channels * width, width * 8)
    if valid_bits is not None:
        fmt += struct.pack("<HHI", 22, valid_bits, (1 << channels) - 1)
        fmt += bytes.fromhex("0100000000001000800000aa00389b71")
    def chunk(key, data):
        return key + struct.pack("<I", len(data)) + data + (b"\xa7" if len(data) & 1 else b"")
    chunks = chunk(b"JUNK", b"abc") + chunk(b"fmt ", fmt)
    chunks += b"data" + struct.pack("<I", 0xFFFFFFFF if rf64 else len(raw)) + raw
    if len(raw) & 1:
        chunks += b"\xe3"
    chunks += chunk(b"LIST", b"custom metadata")
    if rf64:
        ds = struct.pack("<QQQI", 4 + 36 + len(chunks), len(raw), frames, 0)
        result = b"RF64" + b"\xff" * 4 + b"WAVE" + chunk(b"ds64", ds) + chunks
    else:
        result = b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks
    return result + b"original arbitrary trailing bytes\x00\xff"


def dsf_bytes(channel_data: list[bytes], frames: int | None = None,
              bit_order: str = "lsb", rate: int = 2_822_400, padding: int = 0xAD) -> bytes:
    channels = len(channel_data)
    assert len({len(x) for x in channel_data}) == 1 and 1 <= channels <= 6
    frames = frames if frames is not None else len(channel_data[0]) * 8
    assert (frames + 7) // 8 == len(channel_data[0])
    # Explicit arithmetic bit reversal is independent from the module's table.
    def flip(b):
        return sum(((b >> i) & 1) << (7 - i) for i in range(8))
    channels_physical = [bytes(map(flip, data)) if bit_order == "lsb" else data for data in channel_data]
    audio = b"".join(data[i:i + 4096].ljust(4096, bytes([padding]))
                     for i in range(0, len(channel_data[0]), 4096) for data in channels_physical)
    channel_type = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 7}[channels]
    fmt = b"fmt " + struct.pack("<QIIIIIIQII", 52, 1, 0, channel_type, channels,
                                rate, 1 if bit_order == "lsb" else 8, frames, 4096, 0)
    prefix = 28 + len(fmt) + 12
    tail = b"ID3\x04\x00\x00\x00\x00\x00\x0aforeign tag"
    header = b"DSD " + struct.pack("<QQQ", 28, prefix + len(audio) + len(tail), prefix + len(audio))
    return header + fmt + b"data" + struct.pack("<Q", len(audio) + 12) + audio + tail


def dff_bytes(channel_data: list[bytes], rate: int = 2_822_400, compression=b"DSD ") -> bytes:
    channels = len(channel_data)
    assert len({len(x) for x in channel_data}) == 1
    def chunk(key, data):
        return key + struct.pack(">Q", len(data)) + data + (b"\xbe" if len(data) & 1 else b"")
    prop = b"SND " + chunk(b"FS  ", struct.pack(">I", rate))
    prop += chunk(b"CHNL", struct.pack(">H", channels) + b"".join(f"C{i:03d}".encode() for i in range(channels)))
    prop += chunk(b"CMPR", compression + b"\x03raw")
    prop += chunk(b"ABSS", b"foreign")
    raw = b"".join(bytes(frame) for frame in zip(*channel_data))
    body = b"DSD " + chunk(b"FVER", struct.pack(">I", 0x01050000)) + chunk(b"PROP", prop)
    body += chunk(compression, raw) + chunk(b"DIIN", b"arbitrary preserved metadata")
    return b"FRM8" + struct.pack(">Q", len(body)) + body + b"trailing original\x00"


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, data, name="input.bin"):
        p = self.root / name
        p.write_bytes(data)
        return p

    def check_roundtrip(self, source: bytes, expected: list[bytes], *, chunk=269):
        path = self.save(source)
        info = formats.probe(path)
        self.assertIsNotNone(info)
        info = json.loads(json.dumps(info))
        formats.validate_info(info, len(source))
        self.assertEqual(formats.channel_size(info), len(expected[0]))
        for c, data in enumerate(expected):
            pieces = list(formats.iter_channel(path, info, c, chunk))
            self.assertTrue(all(0 < len(x) <= chunk for x in pieces))
            self.assertTrue(all(len(x) % info["width"] == 0 for x in pieces))
            self.assertEqual(b"".join(pieces), data)
        output = self.root / "restored.bin"
        with output.open("w+b") as handle:
            handle.truncate(len(source))
            spans = formats.raw_spans(info, len(source))
            last = 0
            for offset, length in spans:
                self.assertGreaterEqual(offset, last)
                self.assertGreater(length, 0)
                self.assertLessEqual(offset + length, len(source))
                handle.seek(offset)
                handle.write(source[offset:offset + length])
                last = offset + length
            # Reverse channel order and fragmented writes expose accidental overwrites.
            for c in reversed(range(info["channels"])):
                data = expected[c]
                step = 263 - 263 % info["width"]
                for pos in range(0, len(data), step):
                    formats.write_channel(handle, info, c, pos, data[pos:pos + step])
        self.assertEqual(output.read_bytes(), source)
        return info

    def test_pcm_all_widths_and_multichannel_exact_container(self):
        rng = random.Random(617)
        for width in (2, 3, 4):
            for channels in (1, 2, 3, 8):
                with self.subTest(width=width, channels=channels):
                    data = [rng.randbytes(width * 1501) for _ in range(channels)]
                    info = self.check_roundtrip(wav_bytes(data, width), data)
                    self.assertEqual(info["width"], width)

    def test_extensible_valid_bits_do_not_discard_dirty_low_bits(self):
        data = [bytes.fromhex("ffff7f0012345601123456ff00008080"), bytes.fromhex("0000008080000000ffffff7f89abcdef")]
        for valid in (1, 20, 24, 32):
            info = self.check_roundtrip(wav_bytes(data, 4, valid_bits=valid), data)
            self.assertEqual(info["valid_bits"], valid)
            self.assertEqual(info["width"], 4)

    def test_rf64_small_extent_and_container_tails(self):
        data = [bytes(range(252)) * 17, bytes(reversed(range(252))) * 17]
        info = self.check_roundtrip(wav_bytes(data, 3, rf64=True), data)
        self.assertEqual(info["container"], "rf64")

    def test_dsf_bit_order_tail_bits_and_nonzero_padding(self):
        rng = random.Random(97)
        for order in ("lsb", "msb"):
            for count, drop in ((1, 7), (4096, 0), (4097, 5), (8195, 1)):
                with self.subTest(order=order, count=count, drop=drop):
                    data = [rng.randbytes(count), rng.randbytes(count)]
                    info = self.check_roundtrip(dsf_bytes(data, count * 8 - drop, order), data)
                    self.assertEqual(info["bit_order"], order)
                    self.assertEqual(info["frames"], count * 8 - drop)

    def test_dsf_bit_reverse_known_values(self):
        canonical = [bytes([0x00, 0x80, 0x40, 0xC0, 0x20, 0x01, 0xFF])]
        source = dsf_bytes(canonical, bit_order="lsb")
        path = self.save(source)
        info = formats.probe(path)
        self.assertEqual(source[info["data_offset"]:info["data_offset"] + 7], b"\x00\x01\x02\x03\x04\x80\xff")
        self.assertEqual(b"".join(formats.iter_channel(path, info, 0)), canonical[0])

    def test_dff_dsf_same_stream_and_high_rates(self):
        rng = random.Random(611)
        data = [rng.randbytes(8193) for _ in range(3)]
        for rate in (2_822_400, 5_644_800, 11_289_600, 22_579_200):
            for make in (dsf_bytes, dff_bytes):
                with self.subTest(rate=rate, format=make.__name__):
                    info = self.check_roundtrip(make(data, rate=rate), data, chunk=4099)
                    self.assertEqual(info["rate"], rate)

    def test_dff_eight_channels(self):
        self.check_roundtrip(dff_bytes([bytes([i, 255 - i, i * 3]) for i in range(8)]),
                             [bytes([i, 255 - i, i * 3]) for i in range(8)])

    def test_write_changes_only_target_channel(self):
        for make, width in ((wav_bytes, 3), (dsf_bytes, 1), (dff_bytes, 1)):
            a, b = bytes([0x15]) * (4201 * width), bytes([0xAE]) * (4201 * width)
            source = make([a, b])
            path = self.save(source)
            info = formats.probe(path)
            replacement = bytes([0xD2]) * (70 * width)
            at = 4090 * width
            with path.open("r+b") as handle:
                formats.write_channel(handle, info, 1, at, replacement)
            self.assertEqual(b"".join(formats.iter_channel(path, info, 0)), a)
            self.assertEqual(b"".join(formats.iter_channel(path, info, 1)), b[:at] + replacement + b[at + len(replacement):])
            changed = path.read_bytes()
            for off, length in formats.raw_spans(info, len(source)):
                self.assertEqual(changed[off:off + length], source[off:off + length])

    def test_invalid_write_is_rejected_before_mutation(self):
        source = wav_bytes([b"\x01\x02\x03" * 20, b"\xf4\xf5\xf6" * 20])
        path = self.save(source)
        info = formats.probe(path)
        cases = [(False, 0, b"123"), (-1, 0, b"123"), (2, 0, b"123"),
                 (0, -1, b"123"), (0, True, b"123"), (0, 1, b"123"),
                 (0, 0, b"1"), (0, 60, b"123"), (0, 0, bytearray(b"123"))]
        with path.open("r+b") as handle:
            for channel, offset, data in cases:
                with self.assertRaises(ValueError):
                    formats.write_channel(handle, info, channel, offset, data)
        self.assertEqual(path.read_bytes(), source)

    def test_metadata_rejects_untrusted_values(self):
        source = dsf_bytes([b"\xA1" * 17, b"\xD5" * 17], frames=131)
        good = formats.probe(self.save(source))
        for key in good:
            bad = dict(good)
            bad[key] = True
            with self.subTest(key=key), self.assertRaises(ValueError):
                formats.validate_info(bad, len(source))
        mutations = [dict(good, block_size=0), dict(good, frames=(1 << 64)),
                     dict(good, frames=0), dict(good, channels=7), dict(good, width=4),
                     dict(good, data_offset=-1), dict(good, data_size=1),
                     dict(good, data_offset=len(source)), dict(good, bit_order="little"),
                     dict(good, kind="pcm"), dict(good, valid_bits=8), dict(good, extra=0)]
        for bad in mutations:
            with self.assertRaises(ValueError):
                formats.validate_info(bad, len(source))
        bad = dict(good)
        del bad["frames"]
        with self.assertRaises(ValueError):
            formats.validate_info(bad, len(source))

    def test_truncations_declared_sizes_and_float_are_opaque(self):
        sources = [wav_bytes([b"\x01\x02\x03" * 12]),
                   wav_bytes([b"\x01\x02\x03" * 12], rf64=True),
                   dsf_bytes([b"12345", b"56789"]), dff_bytes([b"abc", b"def"])]
        for source in sources:
            info = formats.probe(self.save(source))
            for end in (0, 4, 11, info["data_offset"] - 1,
                        info["data_offset"] + info["data_size"] - 1):
                with self.subTest(magic=source[:4], length=end):
                    self.assertIsNone(formats.probe(self.save(source[:end])))
        wav = bytearray(sources[0])
        fmt = wav.index(b"fmt ") + 8
        struct.pack_into("<H", wav, fmt, 3)
        self.assertIsNone(formats.probe(self.save(wav)))
        dsf = bytearray(sources[2])
        for offset, value in ((32, 0), (52, 7), (60, 2), (64, 0), (72, 1), (76, 1), (80, 1), (84, 1)):
            bad = bytearray(dsf)
            struct.pack_into("<I", bad, offset, value)
            self.assertIsNone(formats.probe(self.save(bad)))
        self.assertIsNone(formats.probe(self.save(dff_bytes([b"abc"], compression=b"DST "))))

    def test_duplicate_and_ambiguous_chunks_are_opaque(self):
        source = wav_bytes([b"\x00\x01" * 10], width=2)
        logical_end = 8 + struct.unpack_from("<I", source, 4)[0]
        additional = b"data\x02\x00\x00\x00\x01\x02"
        bad = bytearray(source[:logical_end] + additional)
        struct.pack_into("<I", bad, 4, len(bad) - 8)
        self.assertIsNone(formats.probe(self.save(bad)))
        source = bytearray(dff_bytes([b"abc"]))
        cmpr = source.index(b"CMPR")
        source[cmpr + 12:cmpr + 16] = b"DST "
        self.assertIsNone(formats.probe(self.save(source)))
        source = bytearray(wav_bytes([b"\x00\x01\x02\x03" * 4], 4, valid_bits=24))
        fmt = source.index(b"fmt ") + 8
        struct.pack_into("<H", source, fmt + 18, 33)
        self.assertIsNone(formats.probe(self.save(source)))

    def test_dsf_unknown_chunks_preserved_but_second_data_rejected(self):
        data = [b"\x00\xff\x13", b"\xA0\xB0\xC0"]
        source = dsf_bytes(data)
        old_metadata = struct.unpack_from("<Q", source, 20)[0]
        extra = b"XTRA" + struct.pack("<Q", 15) + b"\x19\x37\xFF"
        modified = bytearray(source[:80] + extra + source[80:old_metadata] + extra + source[old_metadata:])
        struct.pack_into("<Q", modified, 12, len(modified))
        struct.pack_into("<Q", modified, 20, old_metadata + len(extra) * 2)
        self.check_roundtrip(bytes(modified), data)
        duplicate = b"data" + struct.pack("<Q", 12)
        bad = bytearray(source[:old_metadata] + duplicate + source[old_metadata:])
        struct.pack_into("<Q", bad, 12, len(bad))
        struct.pack_into("<Q", bad, 20, old_metadata + len(duplicate))
        self.assertIsNone(formats.probe(self.save(bad)))

    def test_rf64_hostile_ds64_and_extensible_float(self):
        source = wav_bytes([b"\x00\x01\x02\x03" * 4], 4, rf64=True)
        for offset, pattern in ((4, b"\x00" * 4), (16, b"\xff" * 4),
                                (20, b"\xff" * 8), (28, b"\xff" * 8),
                                (36, b"\x01" + b"\x00" * 7), (44, b"\xff" * 4)):
            bad = bytearray(source)
            bad[offset:offset + len(pattern)] = pattern
            with self.subTest(offset=offset):
                self.assertIsNone(formats.probe(self.save(bad)))
        source = bytearray(wav_bytes([b"\x00\x01\x02\x03" * 4], 4, valid_bits=24))
        guid = source.index(b"fmt ") + 8 + 24
        source[guid] = 3
        self.assertIsNone(formats.probe(self.save(source)))

    def test_truncated_after_probe_and_unbounded_io_arguments(self):
        source = wav_bytes([b"\x00\x01\x02" * 50])
        path = self.save(source)
        info = formats.probe(path)
        for chunk in (0, -1, True, 1, 17 * 1024 * 1024):
            with self.assertRaises(ValueError):
                list(formats.iter_channel(path, info, 0, chunk))
        with path.open("r+b") as handle:
            handle.truncate(info["data_offset"] + 2)
        with self.assertRaises(ValueError):
            list(formats.iter_channel(path, info, 0))
        with path.open("r+b") as handle:
            with self.assertRaises(ValueError):
                formats.write_channel(handle, info, 0, 0, b"\xFF\xFF\xFF")
    def test_rf64_over_4gb_sparse_probe(self):
        data_size = (1 << 32) + 64
        frames = data_size // 8
        header = b"RF64" + b"\xff" * 4 + b"WAVE"
        header += b"ds64" + struct.pack("<IQQQI", 28, data_size + 72, data_size, frames, 0)
        header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 192_000, 192_000 * 8, 8, 32)
        header += b"data" + b"\xff" * 4
        self.assertEqual(len(header), 80)
        path = self.root / "sparse.rf64"
        with path.open("w+b") as handle:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                import msvcrt
                returned = wintypes.DWORD()
                ioctl = ctypes.WinDLL("kernel32", use_last_error=True).DeviceIoControl
                ioctl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                  ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
                ioctl.restype = wintypes.BOOL
                if not ioctl(msvcrt.get_osfhandle(handle.fileno()), 0x900C4,
                             None, 0, None, 0, ctypes.byref(returned), None):
                    self.skipTest("filesystem does not permit safe sparse-file test")
            handle.write(header)
            handle.seek(len(header) + data_size - 1)
            handle.write(b"\x00")
        info = formats.probe(path)
        self.assertIsNotNone(info)
        self.assertEqual(info["data_size"], data_size)
        self.assertEqual(info["frames"], frames)
        self.assertEqual(formats.raw_spans(info, path.stat().st_size), [(0, 80)])


if __name__ == "__main__":
    unittest.main()
