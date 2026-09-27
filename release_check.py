"""Run tests and independently recheck every retained benchmark archive."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

import mastervault


CORE_SOURCE_NAMES = ('mastervault.py', 'formats.py', 'transforms.py',
                     'codecs_layer.py', 'fixtures.py', 'benchmark.py')
LARGE_CHECK_SOURCE_NAMES = ('mastervault.py', 'formats.py', 'transforms.py',
                            'codecs_layer.py', 'large_file_check.py')


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def check_benchmark_sources(results, root):
    """Reject old/nonfrozen measurements before presenting a release as valid."""
    expected = {name: digest(root / name) for name in CORE_SOURCE_NAMES}
    if results.get('source_files_changed_during_run') is not False:
        raise AssertionError('Benchmark did not record an unchanged source snapshot')
    if (results.get('source_sha256_at_start') != expected
            or results.get('source_sha256') != expected):
        raise AssertionError('Benchmark start/end sources do not match the delivered implementation; rerun benchmark')
    return expected


def check_large_file_report(report, root):
    expected = {name: digest(root / name) for name in LARGE_CHECK_SOURCE_NAMES}
    if (report.get('status') != 'passed' or report.get('archive_verified') is not True
            or report.get('full_file_byte_exact') is not True
            or type(report.get('input_bytes')) is not int
            or report['input_bytes'] <= 128 * 1024 * 1024):
        raise AssertionError('Large-file report does not prove the required byte-exact >128 MiB run')
    source_digest = report.get('source_sha256')
    if (type(source_digest) is not str or len(source_digest) != 64
            or any(c not in '0123456789abcdef' for c in source_digest)
            or source_digest != report.get('restored_sha256')):
        raise AssertionError('Large-file source and restored digests differ')
    if report.get('implementation_sha256') != expected:
        raise AssertionError('Large-file measurement uses different implementation sources; rerun large_file_check')


def local_component(value, label):
    if type(value) is not str or '/' in value or '\\' in value:
        raise AssertionError(f'Invalid local {label}')
    mastervault.safe_name(value)
    return value


def main(argv=None):
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, default=root / 'experiment_results_final' / 'results.json')
    parser.add_argument('--output', type=Path, default=root / 'verification.json')
    parser.add_argument('--work-dir', type=Path, default=root / 'validation_scratch')
    parser.add_argument('--large-file-report', type=Path, default=root / 'large_file_check.json')
    parser.add_argument('--require-no-skips', action='store_true')
    args = parser.parse_args(argv)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    source_before = {p.name: digest(p) for p in root.glob('*.py')}
    results_bytes = args.results.read_bytes()
    benchmark_digest = hashlib.sha256(results_bytes).hexdigest()
    results = json.loads(results_bytes)
    benchmark_sources = check_benchmark_sources(results, root)
    large_report_bytes = args.large_file_report.read_bytes()
    large_report_digest = hashlib.sha256(large_report_bytes).hexdigest()
    check_large_file_report(json.loads(large_report_bytes), root)
    start = time.perf_counter()
    log_path = args.output.with_suffix('.tests.log')
    suite = unittest.defaultTestLoader.discover(str(root), pattern='test_*.py')
    with log_path.open('w', encoding='utf-8') as log:
        tests = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    test_seconds = time.perf_counter() - start
    if not tests.wasSuccessful() or (args.require_no_skips and tests.skipped):
        raise AssertionError(f'Tests failed or unexpectedly skipped; see {log_path}')
    if results['all_independent_file_hashes_verified'] is not True:
        raise AssertionError('Benchmark contains an unverified group')
    retained = []
    source_files = []
    for group in results['groups']:
        name = local_component(group['name'], 'group name')
        sources = {}
        for entry in group['files']:
            filename = local_component(entry['name'], 'source filename')
            if filename in sources:
                raise AssertionError('Duplicate source filename in benchmark results')
            path = args.results.parent / 'corpus' / name / filename
            if path.stat().st_size != entry['bytes'] or digest(path) != entry['sha256']:
                raise AssertionError(f'Corpus changed: {path}')
            sources[entry['name']] = path
            source_files.append({'path': f'{name}/{entry["name"]}', 'sha256': entry['sha256'],
                                 'bytes': entry['bytes']})
        if group['independent_file_hashes_verified'] is not True:
            raise AssertionError(f'Group not verified: {name}')
        if group['archive_status'] == 'skipped_no_net_savings':
            if group['mastervault']['archive_bytes'] < group['original_bytes']:
                raise AssertionError('Inconsistent no-gain result')
            continue
        if group['archive_status'] != 'created':
            raise AssertionError('Unknown benchmark archive status')
        # Benchmark filenames are derived from group names. Never interpret an
        # old machine's drive letter, absolute directory, or separator style.
        archive = args.results.parent / 'archives' / f'{name}.mv'
        checked = mastervault.verify(archive, sources, work_dir=args.work_dir)
        if checked['archive_bytes'] != group['mastervault']['archive_bytes']:
            raise AssertionError('Reported archive size differs from actual artifact')
        with tempfile.TemporaryDirectory(prefix='release-', dir=args.work_dir) as folder:
            restored = Path(folder) / 'restored'
            mastervault.unpack(archive, restored)
            actual = {p.relative_to(restored).as_posix() for p in restored.rglob('*') if p.is_file()}
            if actual != set(sources):
                raise AssertionError('Restored filenames differ')
            for filename, source in sources.items():
                if digest(restored / filename) != digest(source):
                    raise AssertionError('Independent restoration digest mismatch')
        retained.append({'group': name, 'archive_bytes': archive.stat().st_size,
                         'archive_sha256': digest(archive), 'verified_against_sources': True,
                         'independent_disk_restoration': True})
    source_after = {p.name: digest(p) for p in root.glob('*.py')}
    if source_before != source_after:
        raise AssertionError('Python sources changed during release validation')
    if digest(args.results) != benchmark_digest or digest(args.large_file_report) != large_report_digest:
        raise AssertionError('Measurement reports changed during release validation')
    report = {'status': 'passed', 'generated_at_utc': datetime.now(timezone.utc).isoformat(),
              'source_sha256': source_after, 'benchmark_sha256': benchmark_digest,
              'benchmark_source_snapshot_verified': True, 'benchmark_source_sha256': benchmark_sources,
              'tests': {'run': tests.testsRun, 'failures': len(tests.failures), 'errors': len(tests.errors),
                        'skipped': len(tests.skipped), 'seconds': test_seconds, 'log': log_path.name},
              'benchmark_groups': len(results['groups']), 'source_files': source_files,
              'retained_archives': retained,
              'dependency_binaries_sha256': {p.name: digest(p) for p in (root / 'tools').glob('*.exe')},
              'large_file_report_sha256': large_report_digest,
              'large_file_report_source_verified': True,
              'total_seconds': time.perf_counter() - start,
              'limits': ['Synthetic corpus only; not real master recordings.',
                         'Growth-only trial archives are verified during benchmark and then discarded.',
                         'No test suite or search proves absence of unknown failures or prior art.']}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': 'passed', 'tests': report['tests'], 'groups': len(results['groups']),
                      'retained_archives_verified': len(retained), 'report': str(args.output)}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
