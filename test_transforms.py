"""Counterexamples, exact round trips, and streaming-boundary checks."""

from __future__ import annotations

import random
import struct
import unittest

from transforms import (
    MAX_BLOCK, canonical_dsd, canonical_pcm, iter_chunks, restore_dsd, restore_pcm,
)
import transforms


def pcm_bytes(samples, width):
    return b"".join(sample.to_bytes(width, "little", signed=True) for sample in samples)


def split_parts(data, pattern=(1, 31, 4097, 3, 16381)):
    yield b""
    position = 0
    index = 0
    while position < len(data):
        size = pattern[index % len(pattern)]
        yield data[position:position + size]
        position += size
        index += 1
    yield b""


def broken_signed32_first(samples):
    """Rejected candidate: signed32 wrapping before dividing by 2**shift."""
    ds = []
    for left, right in zip(samples, samples[1:]):
        d = (right - left) & 0xFFFFFFFF
        ds.append(d - (1 << 32) if d & (1 << 31) else d)
    common = 0
    for d in ds:
        common |= abs(d)
    shift = (common & -common).bit_length() - 1 if common else 0
    p = b"".join(struct.pack("<I", (d >> shift) & 0xFFFFFFFF) for d in ds)
    n = b"".join(struct.pack("<I", (-(d >> shift)) & 0xFFFFFFFF) for d in ds)
    return min(p, n)


class PCMTransformTests(unittest.TestCase):
    def assert_roundtrip(self, samples, width):
        data = pcm_bytes(samples, width)
        payload, meta = canonical_pcm(data, width)
        self.assertEqual(restore_pcm(payload, meta, width), data)
        self.assertEqual(len(payload), 4 * (len(samples) - 1))
        return payload, meta

    def test_all_widths_extreme_transitions(self):
        for width in (2, 3, 4):
            lower, upper = -(1 << (8 * width - 1)), (1 << (8 * width - 1)) - 1
            with self.subTest(width=width):
                self.assert_roundtrip([lower, upper, lower, 0, -1, 1, upper, lower], width)

    def test_random_roundtrips_and_particular_common_powers(self):
        rng = random.Random(42731)
        for width in (2, 3, 4):
            lower, upper = -(1 << (8 * width - 1)), (1 << (8 * width - 1)) - 1
            for length in (1, 2, 3, 9, 127, 4097):
                samples = [rng.randint(lower, upper) for _ in range(length)]
                with self.subTest(width=width, length=length):
                    self.assert_roundtrip(samples, width)
            for shift in range(0, 8 * width - 1):
                self.assert_roundtrip([0, 1 << shift, 0, -(1 << shift)], width)

    def test_single_sample_and_constant_blocks(self):
        for width in (2, 3, 4):
            for value in (0, -1, -(1 << (8 * width - 1)), (1 << (8 * width - 1)) - 1):
                p, m = self.assert_roundtrip([value], width)
                self.assertEqual(p, b"")
                self.assertEqual(m["shift"], 0)
                self.assertEqual(m["sign"], 1)
                p, m = self.assert_roundtrip([value] * 11, width)
                self.assertEqual(p, b"\0" * 40)
                self.assertEqual(m["shift"], 0)

    def test_exact_no_clipping_affine_variants_share_payload(self):
        base = [-211, 9, 113, -51, 83, -71, 64, 7]
        expected, _ = self.assert_roundtrip(base, 2)
        for width in (2, 3, 4):
            for power in (0, 1, 3, 6):
                for sign in (-1, 1):
                    variant = [sign * (x << power) + 101 for x in base]
                    p, _ = self.assert_roundtrip(variant, width)
                    self.assertEqual(p, expected)

    def test_left_aligned_widths_share_payload_at_extremes(self):
        for narrow, wide in ((2, 3), (2, 4), (3, 4)):
            bits = 8 * narrow
            samples = [-(1 << (bits - 1)), (1 << (bits - 1)) - 1, 0, -1, 7, -93]
            expected, _ = self.assert_roundtrip(samples, narrow)
            actual, _ = self.assert_roundtrip([x << (8 * (wide - narrow)) for x in samples], wide)
            self.assertEqual(actual, expected)

    def test_rejected_modulo_before_shift_counterexample(self):
        narrow = [-8388608, 8388607]
        wide = [x << 8 for x in narrow]
        self.assertNotEqual(broken_signed32_first(narrow), broken_signed32_first(wide))
        p24, _ = self.assert_roundtrip(narrow, 3)
        p32, _ = self.assert_roundtrip(wide, 4)
        self.assertEqual(p24, p32)

    def test_signed32_half_modulus_direction_is_retained_before_division(self):
        self.assert_roundtrip([-(1 << 31), 0, -(1 << 31)], 4)
        base, _ = self.assert_roundtrip([-1, 0, -1], 2)
        large, _ = self.assert_roundtrip([-(1 << 31), 0, -(1 << 31)], 4)
        self.assertEqual(base, large)

    def test_low_bits_and_independent_dither_are_preserved(self):
        clean = [0, 256, 768, 1024, -256, 512]
        dirty = [0, 257, 767, 1025, -254, 511]
        pclean, _ = self.assert_roundtrip(clean, 4)
        pdirty, m = self.assert_roundtrip(dirty, 4)
        self.assertNotEqual(pdirty, pclean)
        self.assertEqual(m["shift"], 0)

    def test_clipping_is_a_counterexample_to_gain_invariance(self):
        samples = [-20000, 0, 20000]
        clipped = [max(-32768, min(32767, x * 2)) for x in samples]
        p, _ = self.assert_roundtrip(samples, 2)
        unclipped_p, _ = self.assert_roundtrip([x * 2 for x in samples], 3)
        clipped_p, _ = self.assert_roundtrip(clipped, 2)
        self.assertEqual(p, unclipped_p)
        self.assertNotEqual(p, clipped_p)

    def test_crossing_signed_wrap_is_not_affine_invariance(self):
        samples = [32766, 32767, -32768, -32767]
        wrapped = [((x + 4 + 32768) % 65536) - 32768 for x in samples]
        p, _ = self.assert_roundtrip(samples, 2)
        wrapped_p, _ = self.assert_roundtrip(wrapped, 2)
        self.assertNotEqual(p, wrapped_p)

    def test_empty_partial_and_oversized_pcm_rejected(self):
        for data in (b"", b"x", b"x" * (MAX_BLOCK + 2)):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                canonical_pcm(data, 2)

    def test_width_and_data_types_rejected(self):
        for width in (True, False, 1, 0, 5, 3.0, "3", None):
            with self.subTest(width=width), self.assertRaises(ValueError):
                canonical_pcm(b"\0" * 12, width)
        for data in (bytearray(b"\0\0"), memoryview(b"\0\0"), "aa", None):
            with self.assertRaises(ValueError):
                canonical_pcm(data, 2)

    def test_pcm_metadata_attacks(self):
        payload, meta = canonical_pcm(pcm_bytes([0, 1, -3], 2), 2)
        cases = [None, [], {}, {**meta, "extra": 1}]
        for key in meta:
            missing = dict(meta)
            del missing[key]
            cases.append(missing)
            for value in (True, False, 1.0, "1", None, []):
                cases.append({**meta, key: value})
        for key, value in (("frames", 0), ("frames", -1), ("frames", 10**99),
                           ("anchor", -1), ("anchor", 65536), ("shift", -1),
                           ("shift", 32), ("sign", 0), ("sign", 2)):
            cases.append({**meta, key: value})
        for bad_meta in cases:
            with self.subTest(meta=bad_meta), self.assertRaises(ValueError):
                restore_pcm(payload, bad_meta, 2)
        for bad_payload in (b"", payload[:-1], payload + b"\0", bytearray(payload), None):
            with self.subTest(payload=type(bad_payload)), self.assertRaises(ValueError):
                restore_pcm(bad_payload, meta, 2)


class DSDTransformTests(unittest.TestCase):
    def test_roundtrips_xor_and_complement_invariance(self):
        rng = random.Random(9512)
        for length in (1, 2, 9, 4097, 65536):
            data = rng.randbytes(length)
            p, m = canonical_dsd(data)
            self.assertEqual(restore_dsd(p, m), data)
            for xor in (1, 0x55, 0xA3, 0xFF):
                variant = bytes(x ^ xor for x in data)
                vp, vm = canonical_dsd(variant)
                self.assertEqual(vp, p)
                self.assertEqual(restore_dsd(vp, vm), variant)

    def test_first_byte_anchor_and_all_final_bits_preserved(self):
        for final in range(256):
            data = bytes((0x81, 0x5A, final))
            p, m = canonical_dsd(data)
            self.assertEqual(p[0], 0)
            self.assertEqual(m["anchor"], 0x81)
            self.assertEqual(restore_dsd(p, m), data)

    def test_single_bit_flip_is_not_falsely_identical(self):
        original = b"\x16\x25\x34\x43\x52\x61"
        p, _ = canonical_dsd(original)
        for index in range(len(original)):
            for bit in range(8):
                variant = bytearray(original)
                variant[index] ^= 1 << bit
                vp, vm = canonical_dsd(bytes(variant))
                self.assertNotEqual(vp, p)
                self.assertEqual(restore_dsd(vp, vm), bytes(variant))

    def test_empty_oversize_bad_payload_and_metadata(self):
        for data in (b"", b"x" * (MAX_BLOCK + 1), bytearray(b"a"), None):
            with self.assertRaises(ValueError):
                canonical_dsd(data)
        p, m = canonical_dsd(b"abc")
        for bad in ({}, {"anchor": 0, "extra": 0}, {"anchor": True}, {"anchor": 0.0},
                    {"anchor": -1}, {"anchor": 256}, {"anchor": "1"}, None):
            with self.assertRaises(ValueError):
                restore_dsd(p, bad)
        for bad in (b"", b"\x01ab", b"\0" * (MAX_BLOCK + 1), bytearray(p), None):
            with self.assertRaises(ValueError):
                restore_dsd(bad, m)


class ChunkingTests(unittest.TestCase):
    def test_input_part_boundaries_do_not_affect_chunks(self):
        rng = random.Random(15862)
        raw = rng.randbytes(180123)
        for kind in ("raw", "dsd"):
            expected = list(iter_chunks([raw], kind, average=16384))
            self.assertEqual(list(iter_chunks(split_parts(raw), kind, average=16384)), expected)
            self.assertEqual(b"".join(expected), raw)
        for width in (2, 3, 4):
            data = rng.randbytes(13001 * width)
            expected = list(iter_chunks([data], "pcm", width, average=16384))
            self.assertEqual(list(iter_chunks(split_parts(data), "pcm", width, average=16384)), expected)
            self.assertEqual(b"".join(expected), data)
            self.assertTrue(all(len(chunk) % width == 0 for chunk in expected))

    def test_pcm_different_widths_use_same_sample_grid(self):
        rng = random.Random(81722)
        samples = [rng.randint(-8388608, 8388607) for _ in range(14000)]
        c24 = list(iter_chunks([pcm_bytes(samples, 3)], "pcm", 3, average=16384))
        c32 = list(iter_chunks([pcm_bytes([x << 8 for x in samples], 4)], "pcm", 4, average=16384))
        self.assertEqual([len(x) // 3 for x in c24], [len(x) // 4 for x in c32])
        self.assertEqual([canonical_pcm(x, 3)[0] for x in c24], [canonical_pcm(x, 4)[0] for x in c32])

    def test_complement_has_identical_dsd_boundaries_and_payloads(self):
        data = random.Random(6182).randbytes(700000)
        original = list(iter_chunks([data], "dsd", average=16384))
        for xor in (0xFF, 0x53):
            variant = bytes(x ^ xor for x in data)
            changed = list(iter_chunks(split_parts(variant), "dsd", average=16384))
            self.assertEqual([len(x) for x in changed], [len(x) for x in original])
            self.assertEqual([canonical_dsd(x)[0] for x in changed], [canonical_dsd(x)[0] for x in original])

    def test_one_byte_prefix_insertion_recovers_more_reuse_than_fixed_chunks(self):
        data = random.Random(230912).randbytes(2 * 1024 * 1024)
        altered = b"\x37" + data
        avg = 16384
        base = list(iter_chunks([data], "dsd", average=avg))
        variants = list(iter_chunks(split_parts(altered), "dsd", average=avg))
        known = {canonical_dsd(chunk)[0] for chunk in base}
        cdc_reused = sum(len(chunk) for chunk in variants if canonical_dsd(chunk)[0] in known)
        fixed = [data[offset:offset + avg] for offset in range(0, len(data), avg)]
        fixed_variants = [altered[offset:offset + avg] for offset in range(0, len(altered), avg)]
        fixed_known = {canonical_dsd(chunk)[0] for chunk in fixed}
        fixed_reused = sum(len(chunk) for chunk in fixed_variants if canonical_dsd(chunk)[0] in fixed_known)
        self.assertGreater(cdc_reused, fixed_reused)
        self.assertGreater(cdc_reused, len(data) * 0.85)
        self.assertEqual(b"".join(variants), altered)

    def test_cdc_bounds_and_pathological_forced_bounds(self):
        for kind in ("raw", "dsd"):
            for data in (random.Random(561).randbytes(200000), b"\0" * 200000, b"\xFF" * 200000):
                chunks = list(iter_chunks([data], kind, average=16384))
                self.assertTrue(all(8192 <= len(chunk) <= 65536 for chunk in chunks[:-1]))
                self.assertLessEqual(len(chunks[-1]), 65536)
                self.assertEqual(b"".join(chunks), data)
        # The zero-XOR token stream never hits this table's natural condition:
        # forced boundaries are a real counterexample to universal realignment.
        zero = list(iter_chunks([b"\0" * 200000], "dsd", average=16384))
        self.assertEqual([len(x) for x in zero], [65536, 65536, 65536, 3392])

    def test_adversarial_token_stream_defeats_insertion_reuse(self):
        # White-box counterexample: avoid every natural Gear-mask boundary.
        # This is nonperiodic data, so forced cuts after an inserted byte lose
        # object reuse rather than getting lucky from repeating constant bytes.
        rng = random.Random(239887)
        average = 16384
        mask = average - 1
        fingerprint = transforms._GEAR[0]
        data = bytearray((0x39,))
        for _ in range(3 * average * 4 + 1234 - 1):
            while True:
                token = rng.randrange(256)
                candidate = ((fingerprint << 1) + transforms._GEAR[token]) & ((1 << 64) - 1)
                if candidate & mask:
                    break
            fingerprint = candidate
            data.append(data[-1] ^ token)
        original = list(iter_chunks([bytes(data)], "dsd", average=average))
        inserted = list(iter_chunks([b"\x77" + bytes(data)], "dsd", average=average))
        self.assertEqual([len(x) for x in original[:3]], [65536] * 3)
        known = {canonical_dsd(chunk)[0] for chunk in original}
        reused = sum(len(chunk) for chunk in inserted if canonical_dsd(chunk)[0] in known)
        self.assertEqual(reused, 0)
        self.assertEqual(b"".join(inserted), b"\x77" + bytes(data))

    def test_empty_stream_and_partial_pcm(self):
        for kind, width in (("raw", 1), ("dsd", 1), ("pcm", 2), ("pcm", 3), ("pcm", 4)):
            self.assertEqual(list(iter_chunks([b"", b""], kind, width, average=16384)), [])
        with self.assertRaises(ValueError):
            list(iter_chunks([b"x"], "pcm", 3))
        with self.assertRaises(ValueError):
            list(iter_chunks([b"\0" * (16384 // 4 * 3), b"x"], "pcm", 3, average=16384))

    def test_chunk_parameter_and_part_type_attacks(self):
        for average in (True, False, 0, 8192, 16385, 2**21, 16384.0, "16384", None):
            with self.subTest(average=average), self.assertRaises(ValueError):
                list(iter_chunks([b"abc"], "dsd", average=average))
        for kind in ("wav", "DSD", "", True, 7, None):
            with self.assertRaises(ValueError):
                list(iter_chunks([b"abc"], kind))
        for kind, width in (("raw", 2), ("dsd", 3), ("raw", True), ("pcm", 1), ("pcm", True)):
            with self.assertRaises(ValueError):
                list(iter_chunks([b"abc"], kind, width))
        for part in (bytearray(b"abc"), memoryview(b"abc"), "abc", 1, None):
            with self.assertRaises(ValueError):
                list(iter_chunks([part], "raw"))


if __name__ == "__main__":
    unittest.main()
