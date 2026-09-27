"""Bounded, byte-exact transforms and streaming chunk boundaries.

PCM uses *mathematical signed differences* before removing their common power
of two.  The differences must not be wrapped to signed32 before division: a
24-bit min-to-max transition and its 32-bit left-aligned version otherwise get
different normalized representations.  Python integers make the at-most-33-bit
intermediate subtraction exact.  Only after division are values encoded modulo
2**32.  No valid-bits declaration, rounding, or low-bit truncation is used.

For samples x, let d[i] = x[i] - x[i-1], d[i] = 2**k * q[i], and let
p[i] = s*q[i] mod 2**32, s in {-1,+1}.  Restoration computes
x[i] = x[i-1] + s*p[i]*2**k mod 2**(8*width).  Since s*s = 1 and
2**(8*width) divides 2**32, induction proves exact original bit patterns.
For a nonconstant sequence and a no-clipping integer affine transformation
y = a + epsilon*2**r*x, differences, common powers, and signs cancel, giving
the same canonical payload (when samples and chunk boundaries correspond).
Constant sequences also share their empty/zero residual and retain anchors.
This is not a promise of invariance under signed wraparound, clipping, gain
rounding, resampling, noise, or arbitrary gain.

DSD is opaque canonical-order bytes supplied by the format reader.  Its XOR
differences are invariant under XOR by one constant byte, including complement.
All bits, including a format's non-audible final padding bits, are preserved.

PCM chunking is fixed in sample count: ``average`` is a *32-bit-equivalent byte
budget*, hence each full chunk has average//4 samples for every input width.
DSD/raw use byte CDC.  DSD's rolling tokens are adjacent-byte XORs; previous
byte state continues across chunks, while the rolling hash resets on a cut.
The forced maximum can make adversarial streams align poorly after insertion;
CDC improves expected reuse and does not guarantee insertion invariance.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
import hashlib
import struct


MAX_BLOCK = 4 * 1024 * 1024
MIN_AVERAGE = 16 * 1024
MAX_AVERAGE = 1024 * 1024
_MASK32 = (1 << 32) - 1
_MASK64 = (1 << 64) - 1
_PCM_KEYS = frozenset(("anchor", "shift", "sign", "frames"))
_DSD_KEYS = frozenset(("anchor",))
_GEAR_LABEL = b"MasterVault/adjacent-xor-gear/v1\x00"
_GEAR = tuple(
    int.from_bytes(hashlib.sha256(_GEAR_LABEL + bytes((token,))).digest()[:8], "little")
    for token in range(256)
)


def _check_width(width: int) -> None:
    if type(width) is not int or width not in (2, 3, 4):
        raise ValueError("PCM width must be 2, 3, or 4 bytes")


def _check_bytes(data: bytes, name: str, *, allow_empty: bool = False) -> None:
    if type(data) is not bytes:
        raise ValueError(f"{name} must be bytes")
    if not allow_empty and not data:
        raise ValueError(f"{name} must be nonempty")


def _samples(data: bytes, width: int) -> Iterator[int]:
    if width == 2:
        for sample, in struct.iter_unpack("<h", data):
            yield sample
    elif width == 4:
        for sample, in struct.iter_unpack("<i", data):
            yield sample
    else:
        for offset in range(0, len(data), 3):
            yield int.from_bytes(data[offset:offset + 3], "little", signed=True)


def canonical_pcm(data: bytes, width: int) -> tuple[bytes, dict]:
    """Normalize one nonempty mono integer PCM chunk without losing any bits.

    At most MAX_BLOCK input bytes are accepted.  The returned payload can be
    almost 2*MAX_BLOCK for 16-bit input because residuals occupy four bytes.
    The archive layer must allow that expansion when validating object sizes.
    """
    _check_width(width)
    _check_bytes(data, "PCM data")
    if len(data) > MAX_BLOCK or len(data) % width:
        raise ValueError("PCM data exceeds the block limit or ends in a partial sample")

    frames = len(data) // width
    anchor = int.from_bytes(data[:width], "little")
    source = _samples(data, width)
    previous = next(source)
    common_bits = 0
    for sample in source:
        # Subtract BEFORE any modulo operation.  abs does not change v_2(d).
        common_bits |= abs(sample - previous)
        previous = sample
    shift = (common_bits & -common_bits).bit_length() - 1 if common_bits else 0

    payload_size = 4 * (frames - 1)
    positive = bytearray(payload_size)
    negative = bytearray(payload_size)
    source = _samples(data, width)
    previous = next(source)
    for index, sample in enumerate(source):
        normalized = (sample - previous) >> shift
        struct.pack_into("<I", positive, 4 * index, normalized & _MASK32)
        struct.pack_into("<I", negative, 4 * index, (-normalized) & _MASK32)
        previous = sample
    if positive <= negative:
        payload, sign = bytes(positive), 1
    else:
        payload, sign = bytes(negative), -1
    return payload, {"anchor": anchor, "shift": shift, "sign": sign, "frames": frames}


def restore_pcm(payload: bytes, meta: dict, width: int) -> bytes:
    """Invert a PCM payload, validating shape and bounds before allocation."""
    _check_width(width)
    _check_bytes(payload, "PCM payload", allow_empty=True)
    if type(meta) is not dict or set(meta) != _PCM_KEYS:
        raise ValueError("PCM metadata must contain exactly anchor, shift, sign, frames")
    if any(type(meta[key]) is not int for key in _PCM_KEYS):
        raise ValueError("PCM metadata values must be integers, not bool")
    anchor, shift, sign, frames = (meta[key] for key in ("anchor", "shift", "sign", "frames"))
    if not 1 <= frames <= MAX_BLOCK // width:
        raise ValueError("PCM frames exceed the block limit or are empty")
    if len(payload) != 4 * (frames - 1):
        raise ValueError("PCM payload length does not match frames")
    mask = (1 << (8 * width)) - 1
    if not 0 <= anchor <= mask:
        raise ValueError("PCM anchor does not fit the source width")
    if not 0 <= shift <= 31 or sign not in (-1, 1):
        raise ValueError("invalid PCM shift or sign")

    output = bytearray(frames * width)
    output[:width] = anchor.to_bytes(width, "little")
    sample = anchor
    for index, (normalized,) in enumerate(struct.iter_unpack("<I", payload), start=1):
        sample = (sample + sign * (normalized << shift)) & mask
        offset = index * width
        output[offset:offset + width] = sample.to_bytes(width, "little")
    return bytes(output)


def canonical_dsd(data: bytes) -> tuple[bytes, dict]:
    """Return same-length byte-XOR residuals and the exact first byte."""
    _check_bytes(data, "DSD data")
    if len(data) > MAX_BLOCK:
        raise ValueError("DSD data exceeds the block limit")
    payload = bytearray(len(data))
    previous = data[0]
    for index in range(1, len(data)):
        current = data[index]
        payload[index] = current ^ previous
        previous = current
    return bytes(payload), {"anchor": data[0]}


def restore_dsd(payload: bytes, meta: dict) -> bytes:
    """Restore every original bit, including the final byte's padding bits."""
    _check_bytes(payload, "DSD payload")
    if len(payload) > MAX_BLOCK or payload[0] != 0:
        raise ValueError("DSD payload exceeds the block limit or lacks its zero marker")
    if type(meta) is not dict or set(meta) != _DSD_KEYS:
        raise ValueError("DSD metadata must contain exactly anchor")
    anchor = meta["anchor"]
    if type(anchor) is not int or not 0 <= anchor <= 255:
        raise ValueError("DSD anchor must be an integer byte, not bool")
    output = bytearray(len(payload))
    output[0] = anchor
    previous = anchor
    for index in range(1, len(payload)):
        previous ^= payload[index]
        output[index] = previous
    return bytes(output)


def iter_chunks(
    parts: Iterable[bytes], kind: str, width: int = 1, average: int = 65536
) -> Iterator[bytes]:
    """Chunk concatenated parts independently of the caller's part boundaries.

    ``average`` must be a power of two in [16 KiB, 1 MiB].  For PCM it
    specifies a normalized 32-bit budget: average//4 frames per full chunk.
    For raw/DSD it specifies the CDC mask scale; actual mean size also depends
    on the minimum length and input distribution.  CDC limits are
    max(8192, average//4) and average*4 bytes.  The last chunk may be shorter.
    In raw/DSD mode width must be 1.  An incomplete PCM sample is rejected.
    """
    if type(kind) is not str or kind not in ("raw", "pcm", "dsd"):
        raise ValueError("kind must be raw, pcm, or dsd")
    if type(average) is not int or not MIN_AVERAGE <= average <= MAX_AVERAGE or average & (average - 1):
        raise ValueError("average must be a power of two from 16384 through 1048576")
    if kind == "pcm":
        _check_width(width)
        chunk_size = (average // 4) * width
        pending = bytearray()
        for part in parts:
            _check_bytes(part, "input part", allow_empty=True)
            position = 0
            while position < len(part):
                take = min(chunk_size - len(pending), len(part) - position)
                pending.extend(part[position:position + take])
                position += take
                if len(pending) == chunk_size:
                    yield bytes(pending)
                    pending.clear()
        if len(pending) % width:
            raise ValueError("PCM stream ends in a partial sample")
        if pending:
            yield bytes(pending)
        return

    if type(width) is not int or width != 1:
        raise ValueError("raw and DSD chunking require width=1")
    minimum = max(8192, average // 4)
    maximum = average * 4
    boundary_mask = average - 1
    pending = bytearray()
    fingerprint = 0
    previous = None
    for part in parts:
        _check_bytes(part, "input part", allow_empty=True)
        for current in part:
            if kind == "dsd":
                token = 0 if previous is None else current ^ previous
                previous = current
            else:
                token = current
            pending.append(current)
            fingerprint = ((fingerprint << 1) + _GEAR[token]) & _MASK64
            length = len(pending)
            if length >= minimum and ((fingerprint & boundary_mask) == 0 or length >= maximum):
                yield bytes(pending)
                pending.clear()
                fingerprint = 0
    if pending:
        yield bytes(pending)
