"""Strict, bounded-memory PCM and native DSD container access.

This module was written from the file-format specifications, not external code.
PCM channel streams retain every original little-endian container bit. DSD
channel streams use MSB-first packed bytes, including unused final-byte bits.
Unsupported or ambiguous containers are deliberately left opaque by probe().
"""

from __future__ import annotations

import os
import struct
from collections import deque
from pathlib import Path
from typing import BinaryIO, Iterator


_KEYS = {"container", "kind", "channels", "rate", "frames", "width",
         "valid_bits", "data_offset", "data_size", "block_size", "bit_order"}
_INTEGER_KEYS = _KEYS - {"container", "kind", "bit_order"}
_MAX_OFFSET = (1 << 63) - 1
_MAX_CHUNKS = 100_000
_IO_BYTES = 262_144
_PCM_GUID = bytes.fromhex("0100000000001000800000aa00389b71")
_REVERSE = bytes(int(f"{i:08b}"[::-1], 2) for i in range(256))


def _uint(value, name: str, maximum: int = _MAX_OFFSET) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


def validate_info(info: dict, file_size: int) -> None:
    """Validate untrusted, JSON-roundtripped layout metadata without allocation."""
    _uint(file_size, "file size")
    if type(info) is not dict or set(info) != _KEYS:
        raise ValueError("invalid format metadata keys")
    for name in _INTEGER_KEYS:
        _uint(info[name], name)
    for name in ("container", "kind", "bit_order"):
        if type(info[name]) is not str:
            raise ValueError(f"invalid {name}")
    c, w, n = info["channels"], info["width"], info["frames"]
    if not 1 <= c <= 8 or not 1 <= info["rate"] <= 0xFFFFFFFF or n < 1:
        raise ValueError("invalid channel count, rate or frame count")
    start, length = info["data_offset"], info["data_size"]
    if length < 1 or start + length > file_size:
        raise ValueError("audio range outside file")
    container = info["container"]
    if info["kind"] == "pcm":
        if container not in ("wav", "rf64") or w not in (2, 3, 4):
            raise ValueError("unsupported PCM layout")
        if not 1 <= info["valid_bits"] <= 8 * w:
            raise ValueError("invalid PCM valid bit count")
        if info["block_size"] != 0 or info["bit_order"] != "msb":
            raise ValueError("invalid PCM layout flags")
        if length != n * c * w or start < (44 if container == "wav" else 80):
            raise ValueError("invalid PCM extent")
    elif info["kind"] == "dsd":
        if container not in ("dsf", "dff") or w != 1 or info["valid_bits"] != 1:
            raise ValueError("unsupported DSD layout")
        count = (n + 7) // 8
        if container == "dsf":
            b = info["block_size"]
            if b != 4096 or c > 6 or info["bit_order"] not in ("lsb", "msb"):
                raise ValueError("invalid DSF layout")
            if length != ((count + b - 1) // b) * b * c or start < 92:
                raise ValueError("invalid DSF extent")
        else:
            if (info["block_size"] != 0 or info["bit_order"] != "msb"
                    or n % 8 or length != count * c or start < 16):
                raise ValueError("invalid DFF extent")
    else:
        raise ValueError("unsupported audio kind")


def channel_size(info: dict) -> int:
    """Bytes per normalized channel; callers validate metadata at trust boundaries."""
    if type(info) is not dict or set(info) != _KEYS:
        raise ValueError("invalid format metadata keys")
    validate_info(info, _uint(info["data_offset"], "offset") +
                  _uint(info["data_size"], "size"))
    return info["frames"] * info["width"] if info["kind"] == "pcm" else (info["frames"] + 7) // 8


def _read(handle: BinaryIO, position: int, count: int) -> bytes:
    handle.seek(position)
    result = handle.read(count)
    if len(result) != count:
        raise ValueError("truncated container")
    return result


def _info(container, kind, channels, rate, frames, width, valid_bits,
          data_offset, data_size, block_size=0, bit_order="msb") -> dict:
    return dict(container=container, kind=kind, channels=channels, rate=rate,
                frames=frames, width=width, valid_bits=valid_bits,
                data_offset=data_offset, data_size=data_size,
                block_size=block_size, bit_order=bit_order)


def _wave_fmt(payload: bytes) -> tuple[int, int, int, int]:
    if len(payload) < 16:
        raise ValueError("short WAVE fmt")
    tag, channels, rate, byte_rate, alignment, bits = struct.unpack_from("<HHIIHH", payload)
    if channels not in range(1, 9) or bits not in (16, 24, 32) or rate == 0:
        raise ValueError("unsupported WAVE sample shape")
    width = bits // 8
    if alignment != channels * width or byte_rate != rate * alignment:
        raise ValueError("inconsistent WAVE rates")
    valid = bits
    if tag == 1:
        if len(payload) != 16:
            if len(payload) < 18 or struct.unpack_from("<H", payload, 16)[0] != len(payload) - 18:
                raise ValueError("inconsistent PCM fmt extension")
    elif tag == 0xFFFE:
        if len(payload) < 40 or struct.unpack_from("<H", payload, 16)[0] != len(payload) - 18:
            raise ValueError("invalid extensible fmt size")
        valid, channel_mask = struct.unpack_from("<HI", payload, 18)
        if not 1 <= valid <= bits or payload[24:40] != _PCM_GUID:
            raise ValueError("unsupported extensible subformat")
        if channel_mask and channel_mask.bit_count() != channels:
            raise ValueError("inconsistent extensible channel mask")
    else:
        raise ValueError("non-integer PCM")
    return channels, rate, width, valid


def _wave(handle: BinaryIO, file_size: int, header: bytes) -> dict:
    rf64 = header[:4] == b"RF64"
    if header[8:12] != b"WAVE":
        raise ValueError("not WAVE")
    declared = struct.unpack_from("<I", header, 4)[0]
    entries: dict[bytes, deque[int]] = {}
    ds_data = ds_samples = None
    pos = 12
    if rf64:
        if declared != 0xFFFFFFFF:
            raise ValueError("RF64 requires size sentinel")
        ch = _read(handle, 12, 8)
        size = struct.unpack_from("<I", ch, 4)[0]
        if ch[:4] != b"ds64" or size < 28 or 20 + size + (size & 1) > file_size:
            raise ValueError("missing or truncated ds64")
        riff_size, ds_data, ds_samples, count = struct.unpack("<QQQI", _read(handle, 20, 28))
        if count > _MAX_CHUNKS or size != 28 + 12 * count:
            raise ValueError("invalid ds64 table")
        end = riff_size + 8
        if end > file_size or end < 20 + size:
            raise ValueError("invalid RF64 extent")
        for index in range(count):
            key, value = struct.unpack("<4sQ", _read(handle, 48 + 12 * index, 12))
            if key == b"data":
                raise ValueError("ambiguous additional data size")
            entries.setdefault(key, deque()).append(value)
        pos = 20 + size + (size & 1)
    else:
        if declared == 0xFFFFFFFF:
            raise ValueError("RIFF sentinel without RF64")
        end = declared + 8
        if end > file_size or end < 12:
            raise ValueError("invalid RIFF extent")
    fmt = data = None
    chunks = 0
    while pos < end:
        chunks += 1
        if chunks > _MAX_CHUNKS or end - pos < 8:
            raise ValueError("invalid WAVE chunk sequence")
        ch = _read(handle, pos, 8)
        key, size = struct.unpack("<4sI", ch)
        if key == b"ds64":
            raise ValueError("unexpected ds64")
        if size == 0xFFFFFFFF:
            if not rf64:
                raise ValueError("invalid chunk size sentinel")
            if key == b"data" and data is None:
                size = ds_data
            elif entries.get(key):
                size = entries[key].popleft()
            else:
                raise ValueError("unresolved RF64 size")
        next_pos = pos + 8 + size + (size & 1)
        if next_pos > end:
            raise ValueError("chunk outside WAVE")
        if key == b"fmt ":
            if fmt is not None or size > 65_553:
                raise ValueError("duplicate or excessive fmt")
            fmt = _wave_fmt(_read(handle, pos + 8, size))
        elif key == b"data":
            if data is not None or not size:
                raise ValueError("duplicate or empty data")
            if rf64 and size != ds_data:
                raise ValueError("RF64 data size mismatch")
            data = (pos + 8, size)
        pos = next_pos
    if fmt is None or data is None or any(entries.values()):
        raise ValueError("incomplete WAVE")
    channels, rate, width, valid = fmt
    offset, size = data
    stride = channels * width
    if size % stride:
        raise ValueError("incomplete PCM frame")
    frames = size // stride
    if rf64 and ds_samples not in (0, frames):
        raise ValueError("RF64 sample count mismatch")
    return _info("rf64" if rf64 else "wav", "pcm", channels, rate,
                 frames, width, valid, offset, size)


def _dsf(handle: BinaryIO, file_size: int) -> dict:
    head = _read(handle, 0, 28)
    size, total, metadata = struct.unpack_from("<QQQ", head, 4)
    if head[:4] != b"DSD " or size != 28 or total != file_size:
        raise ValueError("invalid DSF header")
    if metadata and not 28 <= metadata < file_size:
        raise ValueError("invalid DSF metadata pointer")
    fmt = data = None
    pos = 28
    bound = metadata or file_size
    for _ in range(_MAX_CHUNKS):
        if pos == bound:
            if data is None:
                raise ValueError("missing DSF data")
            return data
        if pos + 12 > bound:
            raise ValueError("missing DSF data")
        key, size = struct.unpack("<4sQ", _read(handle, pos, 12))
        if size < 12 or pos + size > bound:
            raise ValueError("invalid DSF chunk extent")
        if key == b"fmt ":
            if fmt is not None or data is not None or size != 52:
                raise ValueError("unsupported DSF fmt")
            version, format_id, channel_type, channels, rate, bits, frames, block, reserved = struct.unpack(
                "<IIIIIIQII", _read(handle, pos + 12, 40))
            counts = {1: 1, 2: 2, 3: 3, 4: 4, 5: 4, 6: 5, 7: 6}
            if (version != 1 or format_id != 0 or counts.get(channel_type) != channels
                    or bits not in (1, 8) or block != 4096 or reserved != 0):
                raise ValueError("unsupported DSF encoding")
            fmt = channels, rate, frames, block, "lsb" if bits == 1 else "msb"
        elif key == b"data":
            if fmt is None or data is not None:
                raise ValueError("DSF data before format or duplicate data")
            channels, rate, frames, block, bit_order = fmt
            data = _info("dsf", "dsd", channels, rate, frames, 1, 1,
                         pos + 12, size - 12, block, bit_order)
        elif key == b"DSD ":
            raise ValueError("duplicate DSF header")
        pos += size
    raise ValueError("too many DSF chunks")


def _dff_chunks(handle: BinaryIO, start: int, end: int):
    pos = start
    for _ in range(_MAX_CHUNKS):
        if pos == end:
            return
        if pos + 12 > end:
            raise ValueError("truncated DFF chunk")
        key, size = struct.unpack(">4sQ", _read(handle, pos, 12))
        following = pos + 12 + size + (size & 1)
        if following > end:
            raise ValueError("DFF chunk outside parent")
        yield key, pos + 12, size
        pos = following
    raise ValueError("too many DFF chunks")


def _dff(handle: BinaryIO, file_size: int) -> dict:
    head = _read(handle, 0, 16)
    if head[:4] != b"FRM8" or head[12:16] != b"DSD ":
        raise ValueError("not DSDIFF")
    size = struct.unpack_from(">Q", head, 4)[0]
    end = size + 12
    if size < 4 or end > file_size:
        raise ValueError("invalid DFF extent")
    version = prop = data = None
    for index, (key, offset, size) in enumerate(_dff_chunks(handle, 16, end)):
        if index == 0 and key != b"FVER":
            raise ValueError("DFF version must be first")
        if key == b"FVER":
            if version is not None or size != 4:
                raise ValueError("invalid DFF version")
            version = struct.unpack(">I", _read(handle, offset, 4))[0]
            if version >> 24 != 1:
                raise ValueError("unsupported DFF version")
        elif key == b"PROP":
            if prop is not None or size < 4 or data is not None or _read(handle, offset, 4) != b"SND ":
                raise ValueError("invalid DFF properties")
            props = {}
            for subkey, suboffset, subsize in _dff_chunks(handle, offset + 4, offset + size):
                if subkey in (b"FS  ", b"CHNL", b"CMPR"):
                    if subkey in props:
                        raise ValueError("duplicate DFF property")
                    if subsize > 1024:
                        raise ValueError("excessive DFF property")
                    props[subkey] = _read(handle, suboffset, subsize)
            if set(props) != {b"FS  ", b"CHNL", b"CMPR"}:
                raise ValueError("missing DFF properties")
            if len(props[b"FS  "]) != 4 or len(props[b"CHNL"]) < 2:
                raise ValueError("bad DFF property size")
            rate = struct.unpack(">I", props[b"FS  "])[0]
            channels = struct.unpack_from(">H", props[b"CHNL"])[0]
            if not 1 <= channels <= 8 or len(props[b"CHNL"]) != 2 + 4 * channels:
                raise ValueError("invalid DFF channels")
            cmpr = props[b"CMPR"]
            if len(cmpr) < 5 or cmpr[:4] != b"DSD " or len(cmpr) != 5 + cmpr[4]:
                raise ValueError("compressed or invalid DFF")
            prop = channels, rate
        elif key == b"DSD ":
            if prop is None or data is not None or not size:
                raise ValueError("ambiguous DFF data")
            data = offset, size
        elif key == b"DST ":
            raise ValueError("DST is opaque")
    if version is None or prop is None or data is None:
        raise ValueError("incomplete DFF")
    channels, rate = prop
    offset, size = data
    if size % channels:
        raise ValueError("partial DFF channel frame")
    return _info("dff", "dsd", channels, rate, size // channels * 8, 1, 1, offset, size)


def probe(path: Path) -> dict | None:
    """Read only structural bytes. I/O errors propagate; bad formats return None."""
    file_size = path.stat().st_size
    if file_size < 12 or file_size > _MAX_OFFSET:
        return None
    try:
        with path.open("rb") as handle:
            header = _read(handle, 0, 12)
            if header[:4] in (b"RIFF", b"RF64"):
                info = _wave(handle, file_size, header)
            elif header[:4] == b"DSD ":
                info = _dsf(handle, file_size)
            elif header[:4] == b"FRM8":
                info = _dff(handle, file_size)
            else:
                return None
        validate_info(info, file_size)
        return info
    except (ValueError, struct.error, OverflowError):
        return None


def _channel_parameters(info: dict, channel: int, chunk_bytes: int, size: int):
    validate_info(info, size)
    if type(channel) is not int or not 0 <= channel < info["channels"]:
        raise ValueError("invalid channel index")
    if type(chunk_bytes) is not int or not 1 <= chunk_bytes <= 16 * 1024 * 1024:
        raise ValueError("invalid I/O chunk length")
    width = info["width"]
    if chunk_bytes < width:
        raise ValueError("chunk length smaller than sample width")
    return chunk_bytes - chunk_bytes % width


def iter_channel(path: Path, info: dict, channel: int,
                 chunk_bytes: int = _IO_BYTES) -> Iterator[bytes]:
    """Yield complete packed channel bytes using at most channels*chunk_bytes RAM."""
    chunk_bytes = _channel_parameters(info, channel, chunk_bytes, path.stat().st_size)
    count = channel_size(info)
    channels, width = info["channels"], info["width"]
    with path.open("rb") as handle:
        offset = 0
        while offset < count:
            take = min(chunk_bytes, count - offset)
            if info["container"] == "dsf":
                result = bytearray()
                while len(result) < take:
                    virtual = offset + len(result)
                    group, within = divmod(virtual, info["block_size"])
                    part = min(take - len(result), info["block_size"] - within)
                    physical = info["data_offset"] + (group * channels + channel) * info["block_size"] + within
                    result.extend(_read(handle, physical, part))
                packed = bytes(result)
                if info["bit_order"] == "lsb":
                    packed = packed.translate(_REVERSE)
            else:
                stride = channels * width
                raw = _read(handle, info["data_offset"] + offset * channels, take * channels)
                if width == 1:
                    packed = raw[channel::channels]
                else:
                    result = bytearray(take)
                    for byte in range(width):
                        result[byte::width] = raw[channel * width + byte::stride]
                    packed = bytes(result)
            yield packed
            offset += take


def raw_spans(info: dict, file_size: int) -> list[tuple[int, int]]:
    """Return a sorted exact complement of the physical channel-byte extents."""
    validate_info(info, file_size)
    start, end = info["data_offset"], info["data_offset"] + info["data_size"]
    spans = [(0, start)]
    if info["container"] == "dsf":
        count = channel_size(info)
        b, channels = info["block_size"], info["channels"]
        used = count % b
        if used:
            last_group = count // b
            for channel in range(channels):
                pad_start = start + (last_group * channels + channel) * b + used
                spans.append((pad_start, b - used))
    if end < file_size:
        spans.append((end, file_size - end))
    merged = []
    for offset, length in spans:
        if not length:
            continue
        if merged and merged[-1][0] + merged[-1][1] == offset:
            old, old_length = merged[-1]
            merged[-1] = old, old_length + length
        else:
            merged.append((offset, length))
    return merged


def write_channel(handle: BinaryIO, info: dict, channel: int,
                  byte_offset: int, data: bytes) -> None:
    """Write normalized bytes to a pre-sized, readable/writable binary file.

    Only bytes belonging to the selected channel are changed. PCM byte_offset
    and data length must be sample aligned. All metadata and range validation
    happens before the first write. The caller owns transactional file handling.
    """
    handle.seek(0, os.SEEK_END)
    file_size = handle.tell()
    _channel_parameters(info, channel, _IO_BYTES, file_size)
    _uint(byte_offset, "channel byte offset")
    if type(data) is not bytes:
        raise ValueError("channel payload must be bytes")
    count, width, channels = channel_size(info), info["width"], info["channels"]
    if byte_offset % width or len(data) % width or byte_offset + len(data) > count:
        raise ValueError("channel write outside extent or not sample aligned")
    cursor = 0
    limit = _IO_BYTES - _IO_BYTES % width
    while cursor < len(data):
        virtual = byte_offset + cursor
        take = min(limit, len(data) - cursor)
        if info["container"] == "dsf":
            group, within = divmod(virtual, info["block_size"])
            take = min(take, info["block_size"] - within)
            physical = info["data_offset"] + (group * channels + channel) * info["block_size"] + within
            payload = data[cursor:cursor + take]
            if info["bit_order"] == "lsb":
                payload = payload.translate(_REVERSE)
        else:
            physical = info["data_offset"] + virtual * channels
            payload = bytearray(_read(handle, physical, take * channels))
            incoming = data[cursor:cursor + take]
            stride = channels * width
            for byte in range(width):
                payload[channel * width + byte::stride] = incoming[byte::width]
        handle.seek(physical)
        written = handle.write(payload)
        if written != len(payload):
            raise OSError("short channel write")
        cursor += take
