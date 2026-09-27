"""Generate a small HiRes example, archive it, and restore every original byte."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import random

import mastervault
from fixtures import pcm_wav, dsf_bytes, dff_bytes


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new directory for demo files')
    parser.add_argument('--codecs', choices=('stdlib', 'auto'), default='stdlib')
    args = parser.parse_args(argv)
    root = args.output.absolute()
    if root.exists():
        parser.error('Use a new output directory; existing files are never overwritten')
    root.mkdir(parents=True)
    source = root / 'sources'
    source.mkdir()
    rng = random.Random(20260927)
    # Exact relationships isolate reversible sharing; these are not recordings.
    samples = [[rng.randrange(-2**21, 2**21), rng.randrange(-2**21, 2**21)]
               for _ in range(16384)]
    (source / 'master24_192k.wav').write_bytes(pcm_wav(samples, 3, 192000))
    aligned = [[value * 256 for value in frame] for frame in samples]
    (source / 'same_master32_192k.wav').write_bytes(pcm_wav(aligned, 4, 192000, valid_bits=24))
    dsd = [rng.randbytes(131072), rng.randbytes(131072)]
    (source / 'master_dsd64.dsf').write_bytes(dsf_bytes(dsd))
    (source / 'same_dsd64.dff').write_bytes(dff_bytes(dsd))
    complement = [bytes(value ^ 255 for value in channel) for channel in reversed(dsd)]
    (source / 'complement_swapped.dff').write_bytes(dff_bytes(complement))
    archive = root / 'example.mva'
    report = mastervault.pack([source], archive, average=16384, codecs=args.codecs,
                              work_dir=root / 'scratch')
    if report['status'] != 'created':
        raise RuntimeError('Expected the deliberately related demo to save space')
    source_map = {'sources/' + path.name: path for path in source.iterdir()}
    report['separate_verification'] = mastervault.verify(archive, source_map)
    restored = root / 'restored'
    report['restoration'] = mastervault.unpack(archive, restored)
    for name, path in source_map.items():
        if path.read_bytes() != (restored / name).read_bytes():
            raise AssertionError('Independent byte comparison failed')
    report['independent_byte_comparison'] = True
    report['synthetic_only'] = True
    report['note'] = 'All source files are retained. Generated data is not a real master recording.'
    (root / 'demo_result.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
