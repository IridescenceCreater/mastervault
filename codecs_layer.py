"""Bounded object codecs. External encoders are reference tools, not new methods."""
from __future__ import annotations
from collections import OrderedDict
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import zlib
import lzma

MAX_OBJECT = 8 * 1024 * 1024
REVERSE_BITS = bytes(int(f'{x:08b}'[::-1], 2) for x in range(256))


class CodecError(ValueError):
    pass


def _bounded_process(command, limit, timeout=45):
    """Read at most limit+1 bytes; kill stalled or expanding external decoders."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    timer = threading.Timer(timeout, process.kill)
    timer.daemon = True
    timer.start()
    try:
        output = process.stdout.read(limit + 1)
        if len(output) > limit:
            process.kill()
            raise CodecError('External codec exceeded the bounded output length')
        code = process.wait(timeout=3)
        if code != 0:
            raise CodecError(f'External codec failed (exit {code}) or exceeded its deadline')
        return output
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def _dsf(data):
    """A temporary mono DSF. Canonical data is MSB-first; DSF uses LSB-first."""
    block = 4096
    padded = data.translate(REVERSE_BITS)
    padded += b'\0' * (-len(padded) % block)
    fmt = b'fmt ' + struct.pack('<QIIIIIIQII', 52, 1, 0, 1, 1, 2822400, 1, len(data)*8, block, 0)
    total = 28 + len(fmt) + 12 + len(padded)
    return b'DSD ' + struct.pack('<QQQ', 28, total, 0) + fmt + b'data' + struct.pack('<Q', 12+len(padded)) + padded


class Codecs:
    def __init__(self, external=True):
        local = Path(__file__).resolve().parent/'tools'
        self.ffmpeg = os.environ.get('FFMPEG_EXE') or shutil.which('ffmpeg')
        self.wavpack = os.environ.get('WAVPACK_EXE') or shutil.which('wavpack') or (str(local/'wavpack.exe') if (local/'wavpack.exe').is_file() else None)
        self.wvunpack = os.environ.get('WVUNPACK_EXE') or shutil.which('wvunpack') or (str(local/'wvunpack.exe') if (local/'wvunpack.exe').is_file() else None)
        self.external = external
        self.rejected_external_candidates = 0
        self._decoded_cache = OrderedDict()
        self._decoded_cache_bytes = 0
        self._decoded_cache_limit = 8*1024*1024

    def available(self):
        return {'ffmpeg': self.ffmpeg, 'wavpack': self.wavpack, 'wvunpack': self.wvunpack,
                'external_encoding_enabled': self.external}

    def require(self, codec):
        if codec == 'flac' and not self.ffmpeg:
            raise CodecError('This archive needs FFmpeg; set FFMPEG_EXE or install it on PATH')
        if codec == 'wavpack-dsd' and not self.wvunpack:
            raise CodecError('This archive needs wvunpack; use bundled tools or set WVUNPACK_EXE')

    def decode(self, data, codec, size, width=1):
        if type(data) is not bytes or type(codec) is not str:
            raise CodecError('Codec input must be bytes with a string codec name')
        if type(size) is not int or not 0 <= size <= MAX_OBJECT:
            raise CodecError('Invalid decoded object size')
        if type(width) is not int or width not in (1, 2, 3, 4):
            raise CodecError('Invalid object width')
        if codec in ('raw','zlib','lzma','wavpack-dsd') and width!=1:
            raise CodecError('Non-PCM codecs must use byte width one')
        if len(data) > MAX_OBJECT + 65536:
            raise CodecError('Encoded object exceeds limit')
        if codec in ('flac','wavpack-dsd'):
            self.require(codec)
        # The full encoded bytes are part of the key, so even a hash collision
        # in Python's dictionary cannot substitute another compressed object.
        cache_key = (codec,width,size,data)
        if cache_key in self._decoded_cache:
            output = self._decoded_cache.pop(cache_key)
            self._decoded_cache[cache_key] = output
            return output
        if codec == 'raw':
            output = data
        elif codec == 'zlib':
            try:
                decoder = zlib.decompressobj()
                output = decoder.decompress(data, size+1)
                if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise CodecError('Invalid or concatenated zlib stream')
            except zlib.error as exc:
                raise CodecError('Invalid zlib object') from exc
        elif codec == 'lzma':
            try:
                decoder = lzma.LZMADecompressor(memlimit=64*1024*1024)
                output = decoder.decompress(data, max_length=size+1)
                if not decoder.eof or decoder.unused_data:
                    raise CodecError('Invalid or concatenated LZMA stream')
            except lzma.LZMAError as exc:
                raise CodecError('Invalid LZMA object or memory limit exceeded') from exc
        elif codec in ('flac', 'wavpack-dsd'):
            self.require(codec)
            with tempfile.TemporaryDirectory(prefix='mastervault-decode-') as tmp:
                source = Path(tmp)/('object.flac' if codec == 'flac' else 'object.wv')
                source.write_bytes(data)
                if codec == 'flac':
                    if width not in (2,3,4) or size % width:
                        raise CodecError('Invalid FLAC object shape')
                    fmt = f's{width*8}le'
                    command = [self.ffmpeg, '-nostdin', '-hide_banner', '-loglevel','error',
                               '-protocol_whitelist','file,pipe','-f','flac','-i',str(source),
                               '-map','0:a:0','-f',fmt,'-c:a',f'pcm_{fmt}','pipe:1']
                    output = _bounded_process(command, size)
                else:
                    if width != 1:
                        raise CodecError('Invalid DSD object width')
                    # --raw never converts DSD to PCM; --raw-pcm is deliberately absent.
                    output = _bounded_process([self.wvunpack, '-q', '--raw', str(source), '-'], size)
                    # WavPack raw DSD is MSB-first regardless of original DSF bit order.
        else:
            raise CodecError('Unknown object codec')
        if len(output) != size:
            raise CodecError('Object expanded to an unexpected length')
        weight = len(data)+len(output)+128
        if weight<=self._decoded_cache_limit:
            while self._decoded_cache and self._decoded_cache_bytes+weight>self._decoded_cache_limit:
                old_key,old_value = self._decoded_cache.popitem(last=False)
                self._decoded_cache_bytes -= len(old_key[3])+len(old_value)+128
            self._decoded_cache[cache_key] = output
            self._decoded_cache_bytes += weight
        return output

    def encode(self, data: bytes, kind='raw', width=1):
        if type(data) is not bytes or type(kind) is not str or kind not in ('raw','pcm','dsd'):
            raise CodecError('Invalid codec input or kind')
        if type(width) is not int or width not in (1,2,3,4):
            raise CodecError('Invalid codec input width')
        if kind=='pcm' and (width not in (2,3,4) or len(data)%width):
            raise CodecError('PCM input must contain complete supported samples')
        if kind in ('raw','dsd') and width!=1:
            raise CodecError('Byte-stream input width must be one')
        if not 0 <= len(data) <= MAX_OBJECT:
            raise CodecError('Object exceeds bounded codec limit')
        choices = [('raw', 1, data), ('zlib', 1, zlib.compress(data, 6))]
        if len(data) >= 256:
            choices.append(('lzma', 1, lzma.compress(data, preset=3)))
        smallest = min(len(c[2]) for c in choices)
        if self.external and data and (kind == 'pcm' and self.ffmpeg or kind == 'dsd' and self.wavpack and self.wvunpack):
            try:
                with tempfile.TemporaryDirectory(prefix='mastervault-encode-') as tmp:
                    folder = Path(tmp)
                    if kind == 'pcm':
                        if width not in (2,3,4) or len(data) % width:
                            raise CodecError('Invalid PCM codec shape')
                        source = folder/'input.raw'
                        source.write_bytes(data)
                        fmt = f's{width*8}le'
                        command = [self.ffmpeg,'-nostdin','-hide_banner','-loglevel','error',
                                   '-f',fmt,'-ar','48000','-ac','1','-i',str(source),
                                   '-map_metadata','-1','-flags','+bitexact','-c:a','flac',
                                   '-strict','experimental',
                                   '-bits_per_raw_sample',str(width*8),'-compression_level','8',
                                   '-metadata_header_padding','0','-f','flac','pipe:1']
                        encoded = _bounded_process(command, len(data)+65536)
                        codec, saved_width = 'flac', width
                    else:
                        source, encoded_path = folder/'input.dsf', folder/'output.wv'
                        source.write_bytes(_dsf(data))
                        subprocess.run([self.wavpack,'-q','-hh','--no-threads',str(source),str(encoded_path)],
                                       stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                       timeout=45,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        if encoded_path.stat().st_size > MAX_OBJECT+65536:
                            raise CodecError('WavPack encoder output exceeds limit')
                        encoded = encoded_path.read_bytes()
                        codec, saved_width = 'wavpack-dsd', 1
                    # A lossy/truncating external tool cannot enter the object store.
                    if len(encoded) < smallest:
                        if self.decode(encoded, codec, len(data), saved_width) != data:
                            raise CodecError('External codec failed exact byte roundtrip')
                        choices.append((codec, saved_width, encoded))
            except (OSError, subprocess.SubprocessError, CodecError):
                self.rejected_external_candidates += 1
        codec, saved_width, encoded = min(choices, key=lambda c: len(c[2]))
        if self.decode(encoded, codec, len(data), saved_width) != data:
            raise CodecError('Selected object failed byte-exact self-check')
        return codec, saved_width, encoded
