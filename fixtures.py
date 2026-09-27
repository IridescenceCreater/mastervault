"""Synthetic high-resolution PCM/native-DSD fixtures, not real master recordings.

All samples are generated locally with fixed seeds.  Rate/bit-depth labels and
valid containers do not establish perceptual quality.  DST-labelled fixtures are
deliberately unsupported parser controls, not valid compressed music.  Only the
Python standard library is used; no reference codec implementation is copied.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import random
import struct


REVERSE = bytes(int(f"{value:08b}"[::-1], 2) for value in range(256))
PCM_GUID = bytes.fromhex("0100000000001000800000aa00389b71")


def riff_chunk(tag: bytes, payload: bytes, pad: bytes = b"\0") -> bytes:
    return tag + struct.pack("<I", len(payload)) + payload + (pad if len(payload) & 1 else b"")


def pcm_wav(samples: list[list[int]], width: int, rate: int = 96000,
            valid_bits: int | None = None, extra_chunks: bool = False) -> bytes:
    """Encode signed integer samples without truncating any container bits."""
    if width not in (2, 3, 4) or not samples or not samples[0]:
        raise ValueError("nonempty 16/24/32-bit integer PCM required")
    channels = len(samples[0])
    if any(len(frame) != channels for frame in samples):
        raise ValueError("inconsistent channel count")
    data = bytearray()
    for frame in samples:
        for value in frame:
            data.extend(value.to_bytes(width, "little", signed=True))
    bits = width * 8
    tag = 1 if valid_bits is None else 0xFFFE
    fmt = struct.pack("<HHIIHH", tag, channels, rate, rate * channels * width, channels * width, bits)
    if valid_bits is not None:
        if not 1 <= valid_bits <= bits:
            raise ValueError("invalid valid_bits")
        fmt += struct.pack("<HHI", 22, valid_bits, 3 if channels == 2 else 4) + PCM_GUID
    chunks = [riff_chunk(b"fmt ", fmt)]
    if extra_chunks:
        chunks.insert(0, riff_chunk(b"JUNK", b"odd metadata!", b"\xa5"))
        chunks.append(riff_chunk(b"LIST", b"INFO" + riff_chunk(b"INAM", b"synthetic master test\0")))
    chunks.append(riff_chunk(b"data", bytes(data)))
    if extra_chunks:
        chunks.append(riff_chunk(b"XTRA", b"\xfeuntouched-after-audio!", b"\x91"))
    body = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def dsf_bytes(channels: list[bytes], rate: int = 2822400, *, lsb: bool = True,
              sample_count: int | None = None, padding: int = 0,
              metadata: bytes = b"") -> bytes:
    """Write DSF from MSB-first canonical channel bytes, preserving all tail bits."""
    if not channels or len(channels) > 6 or len({len(c) for c in channels}) != 1:
        raise ValueError("expected 1..6 equally sized DSD channel byte streams")
    count = len(channels[0]) * 8 if sample_count is None else sample_count
    if count <= 0 or (count + 7) // 8 != len(channels[0]) or not 0 <= padding <= 255:
        raise ValueError("invalid DSD bit count or padding")
    channel_type = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 7}[len(channels)]
    fmt = b"fmt " + struct.pack("<QIIIIIIQII", 52, 1, 0, channel_type, len(channels), rate, 1 if lsb else 8, count, 4096, 0)
    body = bytearray()
    stored = [channel.translate(REVERSE) if lsb else channel for channel in channels]
    for offset in range(0, len(channels[0]), 4096):
        for channel in stored:
            piece = channel[offset:offset + 4096]
            body.extend(piece)
            body.extend(bytes([padding]) * (4096 - len(piece)))
    data = b"data" + struct.pack("<Q", 12 + len(body)) + body
    metadata_offset = 28 + len(fmt) + len(data) if metadata else 0
    total = 28 + len(fmt) + len(data) + len(metadata)
    return b"DSD " + struct.pack("<QQQ", 28, total, metadata_offset) + fmt + data + metadata


def dff_chunk(tag: bytes, payload: bytes, pad: bytes = b"\0") -> bytes:
    return tag + struct.pack(">Q", len(payload)) + payload + (pad if len(payload) & 1 else b"")


def dff_bytes(channels: list[bytes], rate: int = 2822400, *, extra_chunks: bool = False,
              unsupported_dst: bool = False) -> bytes:
    """Write uncompressed DSDIFF; optional DST flag is an invalid-codec control."""
    if not channels or len(channels) > 6 or len({len(c) for c in channels}) != 1:
        raise ValueError("expected equally sized DSD channel byte streams")
    names = (b"SLFT", b"SRGT") if len(channels) == 2 else tuple(f"C{i:03}".encode("ascii") for i in range(len(channels)))
    compression = b"DST " if unsupported_dst else b"DSD "
    description = b"unsupported synthetic DST control" if unsupported_dst else b"not compressed"
    properties = b"SND " + dff_chunk(b"FS  ", struct.pack(">I", rate)) + dff_chunk(b"CHNL", struct.pack(">H", len(channels)) + b"".join(names))
    properties += dff_chunk(b"CMPR", compression + bytes([len(description)]) + description)
    payload = bytearray(len(channels[0]) * len(channels))
    for index, channel in enumerate(channels):
        payload[index::len(channels)] = channel
    chunks = [dff_chunk(b"FVER", struct.pack(">I", 0x01050000)), dff_chunk(b"PROP", properties)]
    if extra_chunks:
        chunks.append(dff_chunk(b"TEST", b"oddly", b"\xf5"))
    if unsupported_dst:
        chunks.append(dff_chunk(b"DST ", dff_chunk(b"FRTE", struct.pack(">IH", 1, 75)) + dff_chunk(b"DSTF", bytes(payload))))
    else:
        chunks.append(dff_chunk(b"DSD ", bytes(payload)))
    if extra_chunks:
        chunks.append(dff_chunk(b"DIIN", dff_chunk(b"DIAR", struct.pack(">I", 9) + b"synthetic")))
    body = b"DSD " + b"".join(chunks)
    return b"FRM8" + struct.pack(">Q", len(body)) + body


def _one_bit_shift(data: bytes) -> bytes:
    """Insert one zero bit and drop the final bit, keeping byte count fixed."""
    return bytes((value >> 1) | ((data[index - 1] & 1) << 7 if index else 0)
                 for index, value in enumerate(data))


def _sigma_delta(length: int, rate: int, frequency: float) -> bytes:
    """First-order toy modulator for codec controls; no master-quality assertion."""
    sine = [round(12000 * math.sin(2 * math.pi * i / 4096)) for i in range(4096)]
    increment = round(frequency * 4096 * (1 << 20) / rate)
    phase = accumulator = 0
    output = bytearray(length)
    for index in range(length):
        packed = 0
        for _ in range(8):
            accumulator += sine[(phase >> 20) & 4095]
            bit = int(accumulator >= 0)
            accumulator -= 32768 if bit else -32768
            packed = (packed << 1) | bit
            phase += increment
        output[index] = packed
    return bytes(output)


def make_corpus(root: Path) -> dict[str, list[Path]]:
    """Create deterministic fixtures, grouped by one experimental purpose each."""
    root = Path(root)
    groups: dict[str, list[Path]] = {}

    def save(group: str, name: str, data: bytes) -> None:
        folder = root / group
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(data)
        groups.setdefault(group, []).append(path)

    for rate in (96000, 192000):
        signal = [[round(2800000 * math.sin(2 * math.pi * 440 * i / rate) + 390000 * math.sin(2 * math.pi * 1777 * i / rate)),
                   round(2300000 * math.sin(2 * math.pi * 660 * (i - 17) / rate))]
                  for i in range(32768)]
        save(f"pcm24_{rate}", f"synth_tonal_24bit_{rate}.wav", pcm_wav(signal, 3, rate, extra_chunks=True))

    rng = random.Random(0x4D415354_24)
    values = [[rng.randrange(-(1 << 20), 1 << 20), rng.randrange(-(1 << 20), 1 << 20)] for _ in range(65536)]
    expanded = [[left << 8, right << 8] for left, right in values]
    transformed = [[(-right + 319) << 8, (left - 197) << 8] for left, right in values]
    save("pcm_bitdepth_versions", "base_24bit_96k.wav", pcm_wav(values, 3, 96000))
    save("pcm_bitdepth_versions", "left_aligned_32valid24_96k.wav", pcm_wav(expanded, 4, 96000, valid_bits=24))
    save("pcm_bitdepth_versions", "swap_polarity_offset_32valid24.wav", pcm_wav(transformed, 4, 96000, valid_bits=24, extra_chunks=True))
    dirty = [[left + rng.randrange(256), right + rng.randrange(256)] for left, right in expanded]
    save("pcm_dirty_lsb", "clean_32valid24.wav", pcm_wav(expanded, 4, 192000, valid_bits=24))
    save("pcm_dirty_lsb", "dirty_lsb_32valid24.wav", pcm_wav(dirty, 4, 192000, valid_bits=24))
    dithered = [[left + rng.randrange(-1, 2), right + rng.randrange(-1, 2)] for left, right in values]
    save("pcm_dither", "undithered_24bit.wav", pcm_wav(values, 3, 96000))
    save("pcm_dither", "integer_dither_preserved_24bit.wav", pcm_wav(dithered, 3, 96000))
    for width in (3, 4):
        low, high = -(1 << (width * 8 - 1)), (1 << (width * 8 - 1)) - 1
        extrema = [[low, high], [high, low], [-1, 0], [0, 1]] * 257
        save("pcm_extremes", f"signed_extremes_{width * 8}.wav", pcm_wav(extrema, width, 192000))
    float_words = [0x00000000, 0x80000000, 0x7FC00001, 0x7FC12345, 0x7F800001, 0xFF800001, 0x7F800000, 0xFF800000, 0x00000001, 0x007FFFFF, 0x3F800000, 0xBF800000]
    float_data = b"".join(struct.pack("<I", word) for word in float_words) * 37
    float_fmt = struct.pack("<HHIIHHH", 3, 1, 192000, 768000, 4, 32, 0)
    float_body = b"WAVE" + riff_chunk(b"fmt ", float_fmt) + riff_chunk(b"fact", struct.pack("<I", len(float_data) // 4)) + riff_chunk(b"data", float_data)
    save("opaque_float", "float32_exact_special_bits.wav", b"RIFF" + struct.pack("<I", len(float_body)) + float_body)

    # Independent, high-entropy channels make ordinary single-file compression
    # ineffective. They are not claimed to be a plausible analogue DSD master.
    rng = random.Random(0x4D415354_D5D)
    channels = [rng.randbytes(1024 * 1024), rng.randbytes(1024 * 1024)]
    save("dsd64_container_versions", "reference_lsb.dsf", dsf_bytes(channels))
    save("dsd64_container_versions", "same_stream_msb.dsf", dsf_bytes(channels, lsb=False))
    save("dsd64_container_versions", "same_stream_interleaved.dff", dff_bytes(channels, extra_chunks=True))
    inverse = [bytes(value ^ 255 for value in channel) for channel in reversed(channels)]
    save("dsd64_complement_versions", "original_before_complement.dsf", dsf_bytes(channels))
    save("dsd64_complement_versions", "complement_and_swap.dff", dff_bytes(inverse))
    save("dsd_byte_edit", "original_before_byte_edit.dsf", dsf_bytes(channels))
    inserted = [rng.randbytes(17) + channel for channel in channels]
    save("dsd_byte_edit", "insert_17_bytes_per_channel.dff", dff_bytes(inserted))
    inserted_inverse = [bytes(value ^ 255 for value in channel) for channel in reversed(inserted)]
    save("dsd_byte_edit", "insert_17_bytes_complement_and_swap.dff", dff_bytes(inserted_inverse))
    save("dsd_bit_shift", "original_before_bit_shift.dsf", dsf_bytes(channels))
    save("dsd_bit_shift", "one_bit_shift.dff", dff_bytes([_one_bit_shift(c) for c in channels]))

    for multiple in (128, 256):
        rate = 44100 * multiple
        shaped = [_sigma_delta(65536, rate, frequency) for frequency in (440, 733)]
        save(f"dsd{multiple}_synth", f"toy_sigma_delta_dsd{multiple}.dsf", dsf_bytes(shaped, rate))
        save(f"dsd{multiple}_synth", f"same_stream_dsd{multiple}.dff", dff_bytes(shaped, rate))
    tails = [bytes([0xAA, 0x96, 0xFF, 0x01, 0xBF]), bytes([0x11, 0x23, 0x45, 0x67, 0xEF])]
    metadata = b"ID3\x04\x00\x00\x00\x00\x00\x00"
    save("dsd_tail_padding", "35bits_lsb_nonzero_padding.dsf", dsf_bytes(tails, sample_count=35, padding=0xA7, metadata=metadata))
    save("dsd_tail_padding", "35bits_msb_nonzero_padding.dsf", dsf_bytes(tails, lsb=False, sample_count=35, padding=0x3C, metadata=metadata))
    save("dsd_noise", "unrelated_uniform_dsd256.dff", dff_bytes([rng.randbytes(256 * 1024), rng.randbytes(256 * 1024)], 44100 * 256))
    save("opaque_dst", "unsupported_synthetic_dst_not_recording.dff", dff_bytes([b"\xA9\x38\x41", b"\x67\xBD\xCA"], unsupported_dst=True))
    save("malformed", "truncated_dsf.dsf", dsf_bytes(tails)[:-7])
    save("malformed", "truncated_dff.dff", dff_bytes(tails)[:-3])
    return groups


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    groups = make_corpus(args.root)
    print("Synthetic fixtures only; not recordings and not a master-quality claim.")
    for name, paths in groups.items():
        print(f"{name}: {len(paths)} files, {sum(p.stat().st_size for p in paths):,} bytes")


if __name__ == "__main__":
    main()
