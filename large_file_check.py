#!/usr/bin/env python3
"""Reproduce a >128 MiB streaming archive test in an isolated Python process.

Uses locally generated repeating 24-bit stereo PCM, not real master recordings.
Reports process peak resident memory and audits every source-file read request.
This tests a bounded-memory execution path, not arbitrary-workload scalability.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import struct
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch


_MIB = 1024 * 1024
_MAX_READ = 8 * _MIB


def implementation_hashes() -> dict[str, str]:
    folder = Path(__file__).resolve().parent
    names = ("mastervault.py", "formats.py", "transforms.py", "codecs_layer.py", "large_file_check.py")
    return {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in names}


def default_work_root() -> Path:
    folder = Path(__file__).resolve().parent
    # In this delivered workspace keep scratch outside outputs. Standalone
    # extracted copies keep it beside the demo instead of climbing to a drive root.
    base = folder.parent.parent if folder.parent.name == "outputs" else folder
    return base / "work" / "mastervault" / "large-file-check"


def peak_resident_bytes() -> tuple[int, str]:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        class MemoryCounters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
        return counters.PeakWorkingSetSize, "Windows GetProcessMemoryInfo.PeakWorkingSetSize"
    import resource
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024), "resource.RUSAGE_SELF.ru_maxrss"


def write_pcm(path: Path, mib: int) -> int:
    data_size = (mib * _MIB // 6) * 6
    if not 128 * _MIB < data_size < 0xFFFFFFFF - 36:
        raise ValueError("test PCM data must exceed 128 MiB and fit RIFF")
    rate = 192_000
    header = b"RIFF" + struct.pack("<I", data_size + 36) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 6, 6, 24)
    header += b"data" + struct.pack("<I", data_size)
    pattern = bytearray()
    for i in range(4096):
        pattern.extend(((i - 2048) * 4095).to_bytes(3, "little", signed=True))
        pattern.extend(((2047 - i) * 3071).to_bytes(3, "little", signed=True))
    slab = bytes(pattern) * 42
    with path.open("xb") as handle:
        handle.write(header)
        left = data_size
        while left:
            part = slab[:min(left, len(slab))]
            handle.write(part)
            left -= len(part)
        handle.flush()
        os.fsync(handle.fileno())
    return data_size + len(header)


class AuditedSource:
    def __init__(self, handle, audit):
        self.handle, self.audit = handle, audit

    def read(self, size=-1):
        if not 0 <= size <= _MAX_READ:
            raise AssertionError(f"source read request is not bounded: {size}")
        self.audit["read_calls"] += 1
        self.audit["largest_requested_read_bytes"] = max(self.audit["largest_requested_read_bytes"], size)
        result = self.handle.read(size)
        self.audit["total_bytes_read_across_passes"] += len(result)
        return result

    def readinto(self, buffer):
        if len(buffer) > _MAX_READ:
            raise AssertionError("source readinto buffer is not bounded")
        self.audit["read_calls"] += 1
        self.audit["largest_requested_read_bytes"] = max(self.audit["largest_requested_read_bytes"], len(buffer))
        count = self.handle.readinto(buffer)
        self.audit["total_bytes_read_across_passes"] += count or 0
        return count

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)


def identical_with_digest(first: Path, second: Path) -> tuple[bool, str, str]:
    a_hash, b_hash = hashlib.sha256(), hashlib.sha256()
    identical = True
    with first.open("rb") as first_handle, second.open("rb") as second_handle:
        while True:
            a, b = first_handle.read(_MIB), second_handle.read(_MIB)
            a_hash.update(a)
            b_hash.update(b)
            identical = identical and a == b
            if not a and not b:
                break
    return identical, a_hash.hexdigest(), b_hash.hexdigest()


def worker(run_dir: Path, mib: int) -> dict:
    implementation = implementation_hashes()
    import mastervault
    from formats import probe
    source, archive, restored = run_dir / "generated_24bit_192k.wav", run_dir / "large.mva", run_dir / "restored"
    scratch = run_dir / "scratch"
    scratch.mkdir()
    generated_start = time.perf_counter()
    original_size = write_pcm(source, mib)
    generation_seconds = time.perf_counter() - generated_start
    info = probe(source)
    if info is None or info["kind"] != "pcm" or info["width"] != 3:
        raise AssertionError("large synthetic input is not recognized PCM")
    audit = dict(read_calls=0, largest_requested_read_bytes=0, total_bytes_read_across_passes=0)
    original_open = Path.open
    def audited_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        return AuditedSource(handle, audit) if path == source and mode == "rb" else handle
    baseline_peak, metric = peak_resident_bytes()
    start = time.perf_counter()
    with patch.object(Path, "open", audited_open):
        t = time.perf_counter()
        report = mastervault.pack([source], archive, average=1_048_576, mode="raw",
                                  codecs="stdlib", work_dir=scratch)
        pack_seconds = time.perf_counter() - t
        if report["status"] != "created":
            raise AssertionError("compressible large input did not create archive")
        t = time.perf_counter()
        verification = mastervault.verify(archive, {source.name: source}, work_dir=scratch)
        verify_seconds = time.perf_counter() - t
        t = time.perf_counter()
        restoration = mastervault.unpack(archive, restored)
        unpack_seconds = time.perf_counter() - t
        t = time.perf_counter()
        same, original_digest, restored_digest = identical_with_digest(source, restored / source.name)
        compare_seconds = time.perf_counter() - t
    elapsed = time.perf_counter() - start
    peak, metric = peak_resident_bytes()
    if not same or not verification["verified"] or not restoration["verified"]:
        raise AssertionError("large file failed byte-exact validation")
    if list(scratch.iterdir()):
        raise AssertionError("archive left temporary build files")
    if implementation_hashes() != implementation:
        raise AssertionError("implementation changed during measurement; rerun on frozen code")
    return {
        "status": "passed", "purpose": "remove prior 128 MiB demo limit; verify a bounded streaming path",
        "synthetic_input": True, "real_master_audio": False,
        "input_bytes": original_size, "pcm_info": info, "archive_bytes": archive.stat().st_size,
        "mode": "raw", "codecs": "stdlib", "average_bytes": 1_048_576,
        "archive_verified": True, "full_file_byte_exact": same,
        "source_sha256": original_digest, "restored_sha256": restored_digest,
        "source_read_audit": audit, "enforced_maximum_source_read_bytes": _MAX_READ,
        "peak_resident_bytes": peak, "peak_memory_metric": metric,
        "peak_resident_before_archive_bytes": baseline_peak,
        "peak_resident_to_input_ratio": round(peak / original_size, 6),
        "timing_seconds": {"generation": round(generation_seconds, 6), "pack_including_internal_verification": round(pack_seconds, 6),
                           "separate_verify": round(verify_seconds, 6), "unpack": round(unpack_seconds, 6),
                           "independent_byte_compare": round(compare_seconds, 6), "archive_work_total": round(elapsed, 6)},
        "objects": report["objects"], "pieces": report["pieces"],
        "implementation_sha256": implementation,
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version, "platform": platform.platform(),
        "limitations": ["Highly repetitive generated PCM is intentionally easy to compress.",
                        "raw mode exercises storage streaming, not PCM semantic normalization throughput.",
                        "Peak memory is for one Python worker process, including imports and codec allocations.",
                        "One workload and size cannot establish asymptotic memory bounds or production performance."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mib", type=int, default=132)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "large_file_check.json")
    parser.add_argument("--work-root", type=Path, default=default_work_root())
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 129 <= args.mib <= 4095:
        parser.error("--mib must be 129..4095 for this RIFF-based >128 MiB check")
    if args.worker:
        if args.run_dir is None:
            parser.error("worker requires run directory")
        print(json.dumps(worker(args.run_dir.resolve(), args.mib), ensure_ascii=True))
        return 0
    args.work_root.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="run-", dir=args.work_root) as folder:
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--run-dir", str(Path(folder).resolve()), "--mib", str(args.mib)]
        process = subprocess.run(command, check=False, capture_output=True, text=True, encoding="utf-8",
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode:
            sys.stderr.write(process.stderr)
            raise RuntimeError(f"isolated large-file worker failed with exit code {process.returncode}")
        result = json.loads(process.stdout)
    result["isolated_worker"] = True
    result["temporary_large_audio_removed"] = True
    result["parent_elapsed_seconds"] = round(time.perf_counter() - started, 6)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report_temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent,
                                         prefix=".large-file-check-", suffix=".json", delete=False) as handle:
            report_temp = Path(handle.name)
            handle.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(report_temp, args.output)
    finally:
        if report_temp is not None:
            report_temp.unlink(missing_ok=True)
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
