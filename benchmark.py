"""Reproducible synthetic MasterVault evaluation with mature codec references.

Use a new output directory: python benchmark.py --output experiment_results
WavPack/WvUnpack use the included official Windows binaries or explicit paths.
FLAC uses an installed FFmpeg; unsupported or non-exact references are reported,
never silently treated as a successful lossless comparison.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import zipfile

from fixtures import make_corpus
from formats import probe, raw_spans

CORE_SOURCE_NAMES = ("mastervault.py", "formats.py", "transforms.py", "codecs_layer.py", "fixtures.py", "benchmark.py")


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_tool(command: list[str]) -> dict:
    start = time.perf_counter()
    try:
        process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=180, check=False,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"command": command, "returncode": process.returncode,
                "seconds": time.perf_counter() - start,
                "stdout": process.stdout.decode("utf-8", errors="replace")[-6000:],
                "stderr": process.stderr.decode("utf-8", errors="replace")[-6000:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "returncode": None, "seconds": time.perf_counter() - start,
                "stdout": "", "stderr": str(exc)}


def _put(archive: zipfile.ZipFile, name: str, data: bytes, *, compressed: bool) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED,
                     compresslevel=9 if compressed else None)


def zip_reference(paths: list[Path], output: Path) -> dict:
    """A real complete ZIP of originals, verified through disk extraction."""
    output.mkdir(parents=True, exist_ok=True)
    target = output / "originals.zip"
    start = time.perf_counter()
    with zipfile.ZipFile(target, "w") as archive:
        for path in sorted(paths):
            _put(archive, path.name, path.read_bytes(), compressed=True)
    encode_seconds = time.perf_counter() - start
    restored = output / "restored"
    restored.mkdir()
    with zipfile.ZipFile(target) as archive:
        if sorted(archive.namelist()) != sorted(path.name for path in paths):
            raise AssertionError("ZIP reference inventory mismatch")
        for path in paths:
            destination = restored / path.name
            with archive.open(path.name) as source, destination.open("wb") as sink:
                shutil.copyfileobj(source, sink, 1024 * 1024)
            if digest_file(destination) != digest_file(path):
                raise AssertionError("ZIP reference changed original bytes")
    return {"status": "complete", "archive_bytes": target.stat().st_size,
            "archive_path": str(target), "verified": True, "encode_seconds": encode_seconds,
            "total_seconds": time.perf_counter() - start, "codec": "ZIP_DEFLATED level 9"}


def wavpack_reference(paths: list[Path], output: Path, encoder: str | None, decoder: str | None) -> dict:
    """Encode whole files; credit a group only if every original file is exact."""
    if not encoder or not decoder:
        return {"status": "unavailable", "archive_bytes": None, "verified": False,
                "reason": "WavPack and WvUnpack are required", "files": []}
    output.mkdir(parents=True, exist_ok=True)
    restored = output / "restored"
    restored.mkdir()
    records, streams, wrapper_members = [], [], []
    started = time.perf_counter()
    for index, source in enumerate(sorted(paths)):
        encoded = output / f"{index:03d}.wv"
        decoded = restored / source.name
        command = [encoder, "-q", "-hh", "-m", "-v", "--no-threads", str(source), "-o", str(encoded)]
        encode = run_tool(command)
        record = {"source": source.name, "source_bytes": source.stat().st_size,
                  "source_sha256": digest_file(source), "encode": encode,
                  "reference_mode": "whole_original_file"}
        info = probe(source)
        if encode["returncode"] != 0 and info is not None and info["kind"] == "pcm":
            record["whole_file_attempts"] = [encode]
            encoded = output / f"{index:03d}-even-depth.wv"
            retry = [encoder, "-q", "-hh", "-m", "-v", "--no-threads", "--force-even-byte-depth", str(source), "-o", str(encoded)]
            encode = run_tool(retry)
            record["whole_file_attempts"].append(encode)
            record["encode"] = encode
        if encode["returncode"] != 0 and info is not None and info["kind"] == "pcm":
            # A container declaring valid24 while carrying nonzero low bits is
            # rejected by whole-WAV WavPack. Encode the exact full-width PCM as
            # raw input and keep every original wrapper span alongside it.
            original = source.read_bytes()
            raw = output / f"{index:03d}-source.pcm"
            raw.write_bytes(original[info["data_offset"]:info["data_offset"] + info["data_size"]])
            encoded = output / f"{index:03d}-raw-pcm.wv"
            decoded_pcm = output / f"{index:03d}-restored.pcm"
            bits = info["width"] * 8
            raw_encode = run_tool([encoder, "-q", "-hh", "-m", "-v", "--no-threads",
                                   f"--raw-pcm={info['rate']},{bits}s,{info['channels']},le", str(raw), "-o", str(encoded)])
            record["raw_pcm_encode"] = raw_encode
            if raw_encode["returncode"] == 0 and encoded.is_file():
                decode = run_tool([decoder, "-q", "--no-threads", "--raw", str(encoded), "-o", str(decoded_pcm)])
                record["decode"] = decode
                if decode["returncode"] == 0 and decoded_pcm.is_file() and decoded_pcm.read_bytes() == raw.read_bytes():
                    restored_bytes = original[:info["data_offset"]] + decoded_pcm.read_bytes() + original[info["data_offset"] + info["data_size"]:]
                    decoded.write_bytes(restored_bytes)
                    if digest_file(decoded) != record["source_sha256"]:
                        raise AssertionError("Raw-PCM WavPack wrapper did not preserve source bytes")
                    spans = []
                    for span_index, (offset, length) in enumerate(raw_spans(info, len(original))):
                        member = f"wrappers/{index:03d}-{span_index}.bin"
                        wrapper_members.append((member, original[offset:offset + length]))
                        spans.append({"offset": offset, "length": length, "member": member})
                    record.update(status="complete", verified=True, encoded_bytes=encoded.stat().st_size,
                                  reference_mode="raw_pcm_plus_exact_wrapper", wrapper_spans=spans,
                                  data_offset=info["data_offset"], data_size=info["data_size"],
                                  raw_format={"rate": info["rate"], "bits": bits, "channels": info["channels"], "endian": "little"})
                    streams.append((f"{source.name}.wv", encoded))
                    records.append(record)
                    continue
            record.update(status="raw_pcm_wrapper_failed", verified=False)
            records.append(record)
            continue
        if encode["returncode"] != 0 or not encoded.is_file():
            record.update(status="encoder_rejected", verified=False)
        else:
            decode = run_tool([decoder, "-q", "--no-threads", str(encoded), "-o", str(decoded)])
            record["decode"] = decode
            if decode["returncode"] != 0 or not decoded.is_file():
                record.update(status="decoder_failed", verified=False)
            elif decoded.stat().st_size != source.stat().st_size or digest_file(decoded) != record["source_sha256"]:
                record.update(status="original_file_bytes_changed", verified=False,
                              restored_sha256=digest_file(decoded), restored_bytes=decoded.stat().st_size)
            else:
                record.update(status="complete", verified=True, encoded_bytes=encoded.stat().st_size)
                streams.append((f"{source.name}.wv", encoded))
        records.append(record)
    complete = all(record["verified"] for record in records)
    result = {"status": "complete" if complete else "not_fully_byte_exact",
              "archive_bytes": None, "verified": complete, "files": records,
              "codec": "whole-file WavPack, -hh -m -v --no-threads; actual executable/version recorded separately",
              "total_seconds": time.perf_counter() - started}
    if complete:
        bundle = output / "whole_file_wavpack.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            for name, encoded in streams:
                _put(archive, name, encoded.read_bytes(), compressed=False)
            for name, payload in wrapper_members:
                _put(archive, name, payload, compressed=True)
            manifest = [{key: value for key, value in record.items() if key not in ("encode", "decode", "whole_file_attempts", "raw_pcm_encode")} for record in records]
            _put(archive, "restore.json", json.dumps(manifest, sort_keys=True).encode(), compressed=True)
        result.update(archive_bytes=bundle.stat().st_size, archive_path=str(bundle),
                      encoded_stream_bytes=sum(path.stat().st_size for _, path in streams))
        with zipfile.ZipFile(bundle) as archive:
            for name, encoded in streams:
                if archive.read(name) != encoded.read_bytes():
                    raise AssertionError("WavPack payload changed during bundling")
            saved = json.loads(archive.read("restore.json"))
            for index, record in enumerate(saved):
                if record["reference_mode"] == "raw_pcm_plus_exact_wrapper":
                    rebuilt = bytearray(record["source_bytes"])
                    for span in record["wrapper_spans"]:
                        payload = archive.read(span["member"])
                        if len(payload) != span["length"]:
                            raise AssertionError("Invalid WavPack wrapper length")
                        rebuilt[span["offset"]:span["offset"] + span["length"]] = payload
                    pcm = (output / f"{index:03d}-restored.pcm").read_bytes()
                    rebuilt[record["data_offset"]:record["data_offset"] + record["data_size"]] = pcm
                    if hashlib.sha256(rebuilt).hexdigest() != record["source_sha256"]:
                        raise AssertionError("Serialized WavPack wrapper cannot reproduce original bytes")
    return result


def flac_reference(paths: list[Path], output: Path, ffmpeg: str | None) -> dict:
    """FLAC for original PCM bytes plus exact original container spans in a ZIP."""
    infos = [probe(path) for path in paths]
    if any(info is None or info["kind"] != "pcm" for info in infos):
        return {"status": "not_applicable", "archive_bytes": None, "verified": False,
                "reason": "This reference requires a group of recognized integer PCM files", "files": []}
    if not ffmpeg:
        return {"status": "unavailable", "archive_bytes": None, "verified": False,
                "reason": "An FFmpeg executable with a FLAC encoder is required", "files": []}
    output.mkdir(parents=True, exist_ok=True)
    restored = output / "restored"
    restored.mkdir()
    bundle = output / "flac_with_exact_wrappers.zip"
    records = []
    started = time.perf_counter()
    with zipfile.ZipFile(bundle, "w") as archive:
        for index, (source, info) in enumerate(zip(paths, infos)):
            assert info is not None
            original = source.read_bytes()
            raw = output / f"{index:03d}.pcm"
            encoded = output / f"{index:03d}.flac"
            decoded = output / f"{index:03d}.decoded.pcm"
            raw.write_bytes(original[info["data_offset"]:info["data_offset"] + info["data_size"]])
            bits = info["width"] * 8
            raw_format = f"s{bits}le"
            encode = run_tool([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", raw_format,
                               "-ar", str(info["rate"]), "-ac", str(info["channels"]), "-i", str(raw),
                               "-c:a", "flac", "-strict", "experimental", "-compression_level", "8", "-sample_fmt", "s16" if bits == 16 else "s32",
                               "-bits_per_raw_sample", str(bits), "-y", str(encoded)])
            record = {"source": source.name, "source_bytes": len(original), "source_sha256": digest_file(source),
                      "data_offset": info["data_offset"], "data_size": info["data_size"],
                      "format": raw_format, "rate": info["rate"], "channels": info["channels"], "encode": encode}
            if encode["returncode"] != 0 or not encoded.is_file():
                record.update(status="encoder_rejected", verified=False)
                records.append(record)
                continue
            decode = run_tool([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(encoded),
                               "-c:a", f"pcm_{raw_format}", "-f", raw_format, "-y", str(decoded)])
            record["decode"] = decode
            if decode["returncode"] != 0 or not decoded.is_file() or decoded.read_bytes() != raw.read_bytes():
                record.update(status="pcm_bytes_not_exact", verified=False)
                records.append(record)
                continue
            spans = []
            restored_path = restored / source.name
            with restored_path.open("w+b") as handle:
                handle.truncate(len(original))
                for span_index, (offset, length) in enumerate(raw_spans(info, len(original))):
                    member = f"{index:03d}/wrapper-{span_index}.bin"
                    payload = original[offset:offset + length]
                    _put(archive, member, payload, compressed=True)
                    spans.append({"offset": offset, "length": length, "member": member})
                    handle.seek(offset)
                    handle.write(payload)
                handle.seek(info["data_offset"])
                with decoded.open("rb") as audio:
                    shutil.copyfileobj(audio, handle)
            if digest_file(restored_path) != record["source_sha256"]:
                raise AssertionError("FLAC wrapper reconstructed different original file bytes")
            member = f"{index:03d}/audio.flac"
            _put(archive, member, encoded.read_bytes(), compressed=False)
            record.update(status="complete", verified=True, audio_member=member, wrapper_spans=spans,
                          encoded_audio_bytes=encoded.stat().st_size)
            records.append(record)
        manifest = [{key: value for key, value in record.items() if key not in ("encode", "decode")} for record in records]
        _put(archive, "restore.json", json.dumps(manifest, sort_keys=True).encode(), compressed=True)
    complete = all(record["verified"] for record in records)
    result = {"status": "complete" if complete else "not_fully_byte_exact", "verified": complete,
              "archive_bytes": bundle.stat().st_size if complete else None, "files": records,
              "codec": "FFmpeg FLAC level 8, -strict experimental for exact 32-bit support + exact source wrapper spans in ZIP",
              "total_seconds": time.perf_counter() - started}
    if complete:
        result["archive_path"] = str(bundle)
        # Verify serialized side-information, rather than trusting only the
        # pre-serialization copy used above for the individual codec check.
        with zipfile.ZipFile(bundle) as archive:
            saved = json.loads(archive.read("restore.json"))
            for record in saved:
                rebuilt = bytearray(record["source_bytes"])
                for span in record["wrapper_spans"]:
                    content = archive.read(span["member"])
                    if len(content) != span["length"]:
                        raise AssertionError("Invalid stored FLAC wrapper length")
                    rebuilt[span["offset"]:span["offset"] + span["length"]] = content
                audio_index = int(record["audio_member"].split("/")[0])
                encoded = output / f"{audio_index:03d}.flac"
                if archive.read(record["audio_member"]) != encoded.read_bytes():
                    raise AssertionError("FLAC payload changed during bundling")
                pcm = (output / f"{audio_index:03d}.decoded.pcm").read_bytes()
                rebuilt[record["data_offset"]:record["data_offset"] + record["data_size"]] = pcm
                if hashlib.sha256(rebuilt).hexdigest() != record["source_sha256"]:
                    raise AssertionError("Serialized FLAC wrapper cannot reproduce the original")
    return result


def choose_tool(explicit: str | None, name: str) -> str | None:
    if explicit:
        path = Path(explicit)
        return str(path.absolute()) if path.is_file() else shutil.which(explicit)
    local = Path(__file__).resolve().parent / "tools" / f"{name}.exe"
    if os.name == "nt" and local.is_file():
        return str(local)
    return shutil.which(name)


def make_report(results: dict) -> str:
    lines = ["# MasterVault 合成语料实验报告", "",
             "全部数据为固定种子合成信号或随机位流，不是真实母带；DSD/HiRes标签不证明音质。本报告只说明已测字节恢复与完整存储成本。理论定位与已有工作见 research.md。",
             "", f"环境：Python {results['environment']['python']}，{results['environment']['platform']}；生成时间 {results['generated_at_utc']}。",
             "", "SQLite真实文件大小计入页、对象、索引、manifest和side-info。成熟对照通常使用完整whole-file WavPack封装ZIP；PCM整文件拒绝时先尝试无损--force-even-byte-depth，再以明确完整容器位宽的raw PCM编码并附原WAV封套，不使用pre-quantize。另测FLAC音频+原WAV封套ZIP。所有可比较对照均已恢复原文件并核对SHA-256。已有压缩流以ZIP_STORED包装，封套/JSON用deflate，计入ZIP目录。只要一个文件最终拒绝或改变原始字节，该组对照标为不可比较，绝不拿部分成功作分母。共享解码程序本体、安装依赖、源文件保留副本和恢复/构建临时空间不计入单包归档大小；需要部署时应另计这些成本。",
             "", "| 语料组 | 原始 B | raw B | native B | semantic B | hybrid B | 选中 B | 选择 | ZIP B | 完整WavPack B | FLAC+封套 B | 恢复 |",
             "|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---|"]
    for group in results["groups"]:
        trial = group["mastervault"]
        candidates = trial.get("candidate_bytes", {})
        refs = group["references"]
        value = lambda reference: f"{reference['archive_bytes']:,}" if reference.get("verified") and reference.get("archive_bytes") is not None else reference["status"]
        sizes = [f"{candidates[name]:,}" if name in candidates else "未提供" for name in ("raw", "native", "semantic", "hybrid")]
        lines.append(f"| {group['name']} | {group['original_bytes']:,} | {' | '.join(sizes)} | {trial['archive_bytes']:,} | {trial.get('strategy', trial.get('selected_mode', '?'))} | {value(refs['zip'])} | {value(refs['wavpack'])} | {value(refs['flac'])} | 通过 |")
    lines.extend(["", "四个策略的完整大小由同一组源文件实测；auto只在这些已构造候选中择小，hybrid的局部新增成本贪心也不等于全局最优。无净收益包经过完整构建/验证后不保留在archives；所有原文件保留。因此执行实验本身不会释放源文件空间。",
                  "", "## 相对成熟对照和实际耗时", "",
                  "| 语料组 | 净省 B | 比WavPack省 B | 比FLAC封套省 B | pack s | verify+unpack+hash s | 保存状态 |",
                  "|---|---:|---:|---:|---:|---:|---|"])
    for group in results["groups"]:
        trial, comparisons = group["mastervault"], group["comparisons"]
        text = lambda value: "不可比较" if value is None else f"{value:,}"
        lines.append(f"| {group['name']} | {group['original_bytes'] - trial['archive_bytes']:,} | {text(comparisons['saving_vs_wavpack_bytes'])} | {text(comparisons['saving_vs_flac_bytes'])} | {group['pack_seconds']:.3f} | {group['verification_seconds']:.3f} | {group['archive_status']} |")
    lines.extend(["", "正数表示MasterVault更小，负数表示成熟对照更小。负结果全部列出。时长为当前机器单次墙钟时间，不是吞吐分布；构建包括选项枚举和自身校验，不能与纯编码时间直接排名。", "", "## 反例与语料解释", "",
                  "- pcm_bitdepth_versions验证24bit、32bit左对齐及精确DC/极性/声道交换；pcm_dirty_lsb与pcm_dither保留全部脏低位和dither，不以validBits宣称可删除。",
                  "- dsd64_container_versions验证DSF两种位序与DFF同流；complement组验证整体反相和声道交换。这里的1MiB/声道高熵随机位流用于隔离共享收益，并非典型模拟DSD录音分布。",
                  "- dsd_byte_edit含原流、各声道前插17字节、以及前插后再complement+交换声道三份，区别普通CDC的编辑复用与变换不变量CDC的组合复用；dsd_bit_shift插1bit并丢末bit，检验byte定位的适用边界。结果不能外推成对任意bit编辑稳定。",
                  "- dsd128_synth与dsd256_synth是简单一阶sigma-delta调制器控制组，不是专业调制器或真实母带音质证据。",
                  "- dsd_tail_padding包含35个有效样本bit、非零无效尾位、不同非零末块padding与metadata。容器恢复必须保留这些非音频字节。",
                  "- opaque_float保留NaN payload、±0、无限和非规格化位；opaque_dst只是明确标名的不可解码合成DST结构控制；malformed为截断格式。它们只能原字节回退，不能声称支持DST编码。",
                  "", "## 对照失败详情", ""])
    failures = 0
    for group in results["groups"]:
        for label in ("wavpack", "flac"):
            ref = group["references"][label]
            if ref["status"] not in ("complete", "not_applicable"):
                failures += 1
                lines.append(f"- {group['name']} / {label}: {ref['status']}。")
                for record in ref.get("files", []):
                    if not record.get("verified"):
                        reason = record.get("decode", record.get("encode", {})).get("stderr", "").replace("\n", " ").strip()
                        lines.append(f"  - {record['source']}: {record['status']}；{reason[:500]}")
    if not failures:
        lines.append("所有适用且可用的成熟编码器对照均逐文件恢复通过。")
    lines.extend(["", "## 执行版本与可复现性", "",
                  results.get("source_hash_note", "本轮source_sha256字段是在运行结束时读取的磁盘源码hash，不是已加载代码的启动snapshot。本轮未记录启动hash，不能追溯断言测试与结束源码完全一致；最终归档应再由最终实现统一校验。"),
                  "", "`results.json`保存逐文件输入SHA-256、四候选真实字节数、策略/编码器统计、原始工具命令和错误、独立恢复结果、源码hash与工具来源。未验证任何首创、无未知反例、普遍优于成熟编码器或真实母带质量结论。", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--average", type=int, default=16384)
    parser.add_argument("--wavpack")
    parser.add_argument("--wvunpack")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--groups", nargs="*", help="optional explicit subset for focused reruns")
    args = parser.parse_args(argv)
    output = args.output.absolute()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("Use a new or empty output directory")
    output.mkdir(parents=True, exist_ok=True)
    encoder, decoder = choose_tool(args.wavpack, "wavpack"), choose_tool(args.wvunpack, "wvunpack")
    ffmpeg = choose_tool(args.ffmpeg, "ffmpeg")
    if encoder:
        os.environ["WAVPACK_EXE"] = encoder
    if decoder:
        os.environ["WVUNPACK_EXE"] = decoder
    if ffmpeg:
        os.environ["FFMPEG_EXE"] = ffmpeg
    source_root = Path(__file__).resolve().parent
    source_hashes_at_start = {name: digest_file(source_root / name) for name in CORE_SOURCE_NAMES}
    import mastervault as mv
    groups = make_corpus(output / "corpus")
    if args.groups:
        unknown = set(args.groups) - set(groups)
        if unknown:
            parser.error(f"Unknown groups: {sorted(unknown)}")
        groups = {name: paths for name, paths in groups.items() if name in args.groups}
    all_results = []
    started = time.perf_counter()
    for name, paths in groups.items():
        print(f"Running {name}: {sum(path.stat().st_size for path in paths):,} bytes", flush=True)
        references_root = output / "references" / name
        refs = {"zip": zip_reference(paths, references_root / "zip"),
                "wavpack": wavpack_reference(paths, references_root / "wavpack", encoder, decoder),
                "flac": flac_reference(paths, references_root / "flac", ffmpeg)}
        trial_archive = output / "measured" / f"{name}.mv"
        trial_archive.parent.mkdir(exist_ok=True)
        start = time.perf_counter()
        stats = mv.pack(paths, trial_archive, average=args.average, mode="auto", allow_growth=True,
                        codecs="auto", work_dir=output / "scratch")
        pack_seconds = time.perf_counter() - start
        if not trial_archive.is_file():
            raise AssertionError("Measured archive was not created with allow_growth=True")
        actual_bytes = trial_archive.stat().st_size
        if stats.get("archive_bytes", actual_bytes) != actual_bytes:
            raise AssertionError("Reported archive length differs from physical SQLite file")
        stats["archive_bytes"] = actual_bytes
        sources = {path.name: path for path in paths}
        start = time.perf_counter()
        verification = mv.verify(trial_archive, compare_sources=sources)
        restored = output / "restored" / name
        unpack = mv.unpack(trial_archive, restored)
        actual_names = {path.relative_to(restored).as_posix() for path in restored.rglob("*") if path.is_file()}
        if actual_names != set(sources):
            raise AssertionError(f"Wrong restored inventory for {name}: {actual_names}")
        for filename, source in sources.items():
            if digest_file(restored / filename) != digest_file(source):
                raise AssertionError(f"Restored file bytes changed: {name}/{filename}")
        verification_seconds = time.perf_counter() - start
        original_bytes = sum(path.stat().st_size for path in paths)
        archive_status, archive_path = "skipped_no_net_savings", None
        if actual_bytes < original_bytes:
            archive_path = output / "archives" / f"{name}.mv"
            archive_path.parent.mkdir(exist_ok=True)
            trial_archive.rename(archive_path)
            archive_status = "created"
        else:
            trial_archive.unlink()
        comparisons = {f"saving_vs_{label}_bytes": ref["archive_bytes"] - actual_bytes if ref.get("verified") and ref.get("archive_bytes") is not None else None for label, ref in refs.items()}
        all_results.append({"name": name, "original_bytes": original_bytes,
                            "files": [{"name": path.name, "bytes": path.stat().st_size, "sha256": digest_file(path), "layout": probe(path)} for path in paths],
                            "mastervault": stats, "references": refs, "comparisons": comparisons,
                            "pack_seconds": pack_seconds, "verification_seconds": verification_seconds,
                            "verification": verification, "unpack": unpack,
                            "independent_file_hashes_verified": True, "archive_status": archive_status,
                            "archive_path": str(archive_path) if archive_path else None})
    source_hashes_at_end = {name: digest_file(source_root / name) for name in CORE_SOURCE_NAMES}
    tool_versions = {}
    for label, executable in (("wavpack", encoder), ("wvunpack", decoder), ("ffmpeg", ffmpeg)):
        if executable:
            version = run_tool([executable, "-version" if label == "ffmpeg" else "--version"])
            tool_versions[label] = version["stdout"].splitlines()[:3]
    results = {"schema_version": 1, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
               "environment": {"python": platform.python_version(), "platform": platform.platform()},
               "tools": {"wavpack": encoder, "wvunpack": decoder, "ffmpeg": ffmpeg},
               "tool_versions": tool_versions,
               "source_sha256": source_hashes_at_end,
               "source_sha256_at_start": source_hashes_at_start,
               "source_files_changed_during_run": source_hashes_at_start != source_hashes_at_end,
               "source_hash_note": ("本轮在主模块导入前与结束时记录并核对mastervault.py、formats.py、transforms.py、codecs_layer.py、fixtures.py、benchmark.py的SHA-256；六文件起止完全一致。README、启动器和验收脚本不属于该算法/实验冻结集合。source_sha256为结束值，source_sha256_at_start为开始值。" if source_hashes_at_start == source_hashes_at_end else "本轮记录了六个算法/实验文件的起止SHA-256，但运行期间发生变化；不能把结束hash当作已加载版本，本轮不能作为冻结版本的最终基准。"),
               "average": args.average, "elapsed_seconds": time.perf_counter() - started,
               "synthetic_only": True, "groups": all_results,
               "all_independent_file_hashes_verified": all(row["independent_file_hashes_verified"] for row in all_results)}
    (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    (output / "report.md").write_text(make_report(results), encoding="utf-8")
    print(json.dumps({"groups": len(all_results), "verified": results["all_independent_file_hashes_verified"], "seconds": results["elapsed_seconds"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
