#!/usr/bin/env python3
"""MasterVault: streaming, byte-exact HiRes PCM / native DSD archive prototype."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import tempfile
import unicodedata

from codecs_layer import Codecs, CodecError, MAX_OBJECT
from formats import probe, validate_info, channel_size, iter_channel, raw_spans, write_channel
from transforms import canonical_pcm, restore_pcm, canonical_dsd, restore_dsd, iter_chunks

VERSION = 'mastervault-1'
MAX_TOTAL = 64 * 1024**4
MAX_FILES = 10000
MAX_OBJECTS = 2000000
MAX_PIECES = 8000000
IO_BLOCK = 1024*1024
SCHEMA = {
    'meta': 'CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID',
    'files': 'CREATE TABLE files(id INTEGER PRIMARY KEY,path TEXT NOT NULL UNIQUE,size INTEGER NOT NULL,sha256 TEXT NOT NULL,info TEXT NOT NULL)',
    'recipes': 'CREATE TABLE recipes(id INTEGER PRIMARY KEY,file_id INTEGER NOT NULL,kind TEXT NOT NULL)',
    'streams': 'CREATE TABLE streams(id INTEGER PRIMARY KEY,recipe_id INTEGER NOT NULL,role TEXT NOT NULL,channel INTEGER NOT NULL,offset INTEGER NOT NULL,size INTEGER NOT NULL)',
    'pieces': 'CREATE TABLE pieces(stream_id INTEGER NOT NULL,ordinal INTEGER NOT NULL,object_id INTEGER NOT NULL,transform TEXT NOT NULL,params TEXT NOT NULL,size INTEGER NOT NULL,PRIMARY KEY(stream_id,ordinal)) WITHOUT ROWID',
    'objects': 'CREATE TABLE objects(id INTEGER PRIMARY KEY,bucket TEXT NOT NULL,sha256 TEXT NOT NULL,size INTEGER NOT NULL,codec TEXT NOT NULL,width INTEGER NOT NULL,encoded_sha256 TEXT NOT NULL,data BLOB NOT NULL)',
}
INDEXES = {
    'object_hash': 'CREATE INDEX object_hash ON objects(bucket,size)',
    'piece_objects': 'CREATE INDEX piece_objects ON pieces(object_id)',
    'recipe_files': 'CREATE INDEX recipe_files ON recipes(file_id)',
    'stream_recipes': 'CREATE INDEX stream_recipes ON streams(recipe_id)',
}


class ArchiveError(ValueError):
    pass


def _json(data):
    return json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=True)


def _pairs(pairs):
    result = {}
    for key,value in pairs:
        if key in result:
            raise ArchiveError('Duplicate JSON key')
        result[key] = value
    return result


def _parse(text, limit=8192):
    if type(text) is not str or len(text) > limit:
        raise ArchiveError('Oversized JSON metadata')
    try:
        return json.loads(text,object_pairs_hook=_pairs)
    except (ValueError,RecursionError) as exc:
        raise ArchiveError('Invalid JSON metadata') from exc


def _int(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ArchiveError(f'Invalid {name}')
    return value


def sha(data):
    return hashlib.sha256(data).hexdigest()


def bucket_hash(data):
    return sha(data)  # Index only; collision hits are compared as actual bytes.


def _digest(value):
    if type(value) is not str or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
        raise ArchiveError('Invalid SHA256 field')


def file_sha(path):
    path = Path(path)
    remaining = path.stat().st_size
    value = hashlib.sha256()
    with path.open('rb') as handle:
        while remaining:
            data = handle.read(min(IO_BLOCK,remaining))
            if not data:
                raise ArchiveError('File was truncated during hashing')
            remaining -= len(data)
            value.update(data)
        if handle.read(1):
            raise ArchiveError('File grew during hashing')
    return value.hexdigest()


def safe_name(name):
    if type(name) is not str or not name or len(name)>220 or '\\' in name:
        raise ArchiveError('Unsafe or long archive path')
    parts = name.split('/')
    if len(parts)>16:
        raise ArchiveError('Too many path components')
    devices = {'CON','PRN','AUX','NUL','CONIN$','CONOUT$'} | {p+n for p in ('COM','LPT') for n in '123456789¹²³'}
    for part in parts:
        if not part or part in ('.','..') or part[-1] in ' .':
            raise ArchiveError('Unsafe path component')
        if any(c in '<>:"|?*' or unicodedata.category(c) in ('Cc','Cs') for c in part):
            raise ArchiveError('Unsafe filename character')
        if part.split('.')[0].rstrip(' ').upper() in devices:
            raise ArchiveError('Reserved device filename')
    return unicodedata.normalize('NFC',name).casefold()


def validate_names(names):
    normalized = [safe_name(n) for n in names]
    seen = set(normalized)
    if len(seen)!=len(normalized):
        raise ArchiveError('Duplicate/case/Unicode conflicting names')
    for name in seen:
        parts = name.split('/')
        if any('/'.join(parts[:i]) in seen for i in range(1,len(parts))):
            raise ArchiveError('File/directory path collision')


def no_links(path):
    path = Path(path).absolute()
    for item in (path,*path.parents):
        if item.is_symlink() or (hasattr(item,'is_junction') and item.is_junction()):
            raise ArchiveError('Symlinks/junctions are not accepted')


def collect_sources(sources):
    entries = []
    def fail(error):
        raise error
    def add(name,path):
        no_links(path)
        if not stat.S_ISREG(path.stat().st_mode):
            raise ArchiveError('Input is not a regular file')
        entries.append((name,path))
        if len(entries)>MAX_FILES:
            raise ArchiveError('File count limit exceeded')
    for value in sources:
        source = Path(value).absolute()
        no_links(source)
        if source.is_dir():
            for folder,dirs,files in os.walk(source,followlinks=False,onerror=fail):
                for name in dirs:
                    no_links(Path(folder)/name)
                for name in files:
                    path = Path(folder)/name
                    add(source.name+'/'+path.relative_to(source).as_posix(),path)
        elif source.is_file():
            add(source.name,source)
        else:
            raise ArchiveError(f'Input does not exist: {source}')
    if not entries:
        raise ArchiveError('No input files')
    validate_names([n for n,_ in entries])
    if sum(p.stat().st_size for _,p in entries)>MAX_TOTAL:
        raise ArchiveError('Total source size exceeds the 64 TiB format limit')
    return sorted(entries)


def _snapshot(path):
    item = path.stat()
    return item.st_size,item.st_mtime_ns,item.st_ino


def _new_database(path):
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=DELETE')
    connection.execute('PRAGMA synchronous=FULL')
    connection.execute('PRAGMA cache_size=-8192')
    connection.execute('PRAGMA temp_store=FILE')
    for statement in (*SCHEMA.values(),*INDEXES.values()):
        connection.execute(statement)
    connection.commit()
    return connection


@contextmanager
def _read_database(path):
    path = Path(path).absolute()
    no_links(path)
    if not path.is_file() or path.stat().st_size<4096 or path.stat().st_size>MAX_TOTAL*8:
        raise ArchiveError('Invalid archive file size')
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)
        connection.execute('PRAGMA trusted_schema=OFF')
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA cache_size=-8192')
        connection.execute('PRAGMA mmap_size=0')
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,MAX_OBJECT+131072)
        schema_count = connection.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]
        if schema_count>32 or connection.execute('SELECT 1 FROM sqlite_master WHERE length(name)>128 OR length(sql)>2048 LIMIT 1').fetchone():
            raise ArchiveError('Oversized database schema')
        rows = connection.execute('SELECT type,name,tbl_name,sql FROM sqlite_master').fetchall()
        expected = {**SCHEMA,**INDEXES}
        found = {}
        for kind,name,table,statement in rows:
            if kind=='index' and statement is None and name.startswith('sqlite_autoindex_') and table in SCHEMA:
                continue
            if name not in expected or statement != expected[name] or kind not in ('table','index'):
                raise ArchiveError('Unsupported database schema, view, or trigger')
            found[name] = statement
        if found != expected:
            raise ArchiveError('Incomplete archive schema')
        yield connection
    except sqlite3.Error as exc:
        raise ArchiveError(f'Invalid archive database: {exc}') from exc
    finally:
        if connection is not None:
            connection.close()


def _read_range(path,offset,length):
    with path.open('rb') as handle:
        handle.seek(offset)
        remaining = length
        while remaining:
            part = handle.read(min(IO_BLOCK,remaining))
            if not part:
                raise ArchiveError('Source changed or was truncated while reading')
            remaining -= len(part)
            yield part


def _object_bytes(row, codecs):
    _,bucket,digest,size,codec,width,encoded_digest,data = row
    _int(size,0,MAX_OBJECT,'object size')
    _digest(digest)
    _digest(encoded_digest)
    if type(data) is not bytes or len(data)>MAX_OBJECT+65536 or sha(data)!=encoded_digest:
        raise ArchiveError('Encoded object checksum/size mismatch')
    decoded = codecs.decode(data,codec,size,width)
    if sha(decoded)!=digest:
        raise ArchiveError('Decoded object checksum mismatch')
    return decoded


class Pool:
    def __init__(self,path,codecs):
        self.db = _new_database(path)
        self.db.execute('CREATE TABLE codec_attempts(object_id INTEGER,kind TEXT,width INTEGER,PRIMARY KEY(object_id,kind,width)) WITHOUT ROWID')
        self.codecs = codecs
        self.objects = self.pieces = 0

    def add_object(self,data,kind='raw',width=1):
        lookup = bucket_hash(data)
        # A deliberately corrupted index or mocked collision cannot merge bytes.
        for row in self.db.execute('SELECT * FROM objects WHERE bucket=? AND size=?',(lookup,len(data))):
            if _object_bytes(row,self.codecs)==data:
                attempted = self.db.execute('SELECT 1 FROM codec_attempts WHERE object_id=? AND kind=? AND width=?',(row[0],kind,width)).fetchone()
                if not attempted:
                    codec,saved_width,encoded = self.codecs.encode(data,kind,width)
                    self.db.execute('INSERT INTO codec_attempts VALUES(?,?,?)',(row[0],kind,width))
                    if len(encoded)<len(row[-1]):
                        self.db.execute('UPDATE objects SET codec=?,width=?,encoded_sha256=?,data=? WHERE id=?',(codec,saved_width,sha(encoded),encoded,row[0]))
                return row[0]
        self.objects += 1
        if self.objects>MAX_OBJECTS:
            raise ArchiveError('Object count exceeds supported limit')
        codec,saved_width,encoded = self.codecs.encode(data,kind,width)
        cursor = self.db.execute('INSERT INTO objects(bucket,sha256,size,codec,width,encoded_sha256,data) VALUES(?,?,?,?,?,?,?)',
                                 (lookup,sha(data),len(data),codec,saved_width,sha(encoded),encoded))
        identity = cursor.lastrowid
        self.db.execute('INSERT INTO codec_attempts VALUES(?,?,?)',(identity,kind,width))
        return identity

    def stream(self,recipe,role,channel,offset,size,parts,chunk_kind,average,transform='raw',width=1,audio_kind='raw'):
        stream = self.db.execute('INSERT INTO streams(recipe_id,role,channel,offset,size) VALUES(?,?,?,?,?)',
                                (recipe,role,channel,offset,size)).lastrowid
        actual = 0
        for ordinal,block in enumerate(iter_chunks(parts,chunk_kind,width,average)):
            actual += len(block)
            if transform=='pcm':
                payload,params = canonical_pcm(block,width)
                if restore_pcm(payload,params,width)!=block:
                    raise ArchiveError('PCM transformation self-check failed')
                object_kind,object_width = 'pcm',4
            elif transform=='dsd':
                payload,params = canonical_dsd(block)
                if restore_dsd(payload,params)!=block:
                    raise ArchiveError('DSD transformation self-check failed')
                # A transition stream is stored as bytes, not presented as an audio master.
                object_kind,object_width = 'raw',1
            else:
                payload,params = block,{}
                object_kind,object_width = audio_kind,width
            obj = self.add_object(payload,object_kind,object_width)
            self.pieces += 1
            if self.pieces>MAX_PIECES:
                raise ArchiveError('Piece count limit exceeded')
            self.db.execute('INSERT INTO pieces VALUES(?,?,?,?,?,?)',(stream,ordinal,obj,transform,_json(params),len(block)))
        if actual!=size:
            raise ArchiveError('Input stream length changed or format layout is inconsistent')

    def file(self,file_id,path,info,average,strategies):
        size = path.stat().st_size
        for strategy in strategies:
            if strategy!='raw' and info is None:
                continue
            recipe = self.db.execute('INSERT INTO recipes(file_id,kind) VALUES(?,?)',(file_id,strategy)).lastrowid
            if strategy=='raw':
                self.stream(recipe,'raw',-1,0,size,_read_range(path,0,size),'raw',average)
            else:
                for offset,length in raw_spans(info,size):
                    self.stream(recipe,'raw',-1,offset,length,_read_range(path,offset,length),'raw',average)
                for channel in range(info['channels']):
                    kind,width = info['kind'],info['width']
                    chunk_kind = kind if strategy=='semantic' or kind=='pcm' else 'raw'
                    transform = kind if strategy=='semantic' else 'raw'
                    self.stream(recipe,'channel',channel,0,channel_size(info),iter_channel(path,info,channel),
                                chunk_kind,average,transform,width,kind)
        self.db.commit()


def _recipe_cost(db,recipe):
    data = db.execute('SELECT coalesce(sum(length(o.data)+112),0) FROM objects o WHERE o.id IN '
                      '(SELECT p.object_id FROM pieces p JOIN streams s ON p.stream_id=s.id WHERE s.recipe_id=?) '
                      'AND o.id NOT IN (SELECT id FROM chosen_objects)',(recipe,)).fetchone()[0]
    metadata = db.execute('SELECT coalesce(sum(112+length(p.params)),0) FROM pieces p JOIN streams s ON p.stream_id=s.id WHERE s.recipe_id=?',(recipe,)).fetchone()[0]
    streams = db.execute('SELECT count(*) FROM streams WHERE recipe_id=?',(recipe,)).fetchone()[0]
    return data+metadata+streams*96


def _select(db,policy):
    db.execute('DROP TABLE IF EXISTS temp.selected_recipes')
    db.execute('DROP TABLE IF EXISTS temp.chosen_objects')
    db.execute('CREATE TEMP TABLE selected_recipes(id INTEGER PRIMARY KEY)')
    db.execute('CREATE TEMP TABLE chosen_objects(id INTEGER PRIMARY KEY)')
    for file_id, in db.execute('SELECT id FROM files ORDER BY id').fetchall():
        recipes = dict(db.execute('SELECT kind,id FROM recipes WHERE file_id=?',(file_id,)))
        if policy=='hybrid':
            selected = min(recipes.values(),key=lambda r:(_recipe_cost(db,r),r))
        else:
            selected = recipes.get(policy,recipes['raw'])
        db.execute('INSERT INTO selected_recipes VALUES(?)',(selected,))
        db.execute('INSERT OR IGNORE INTO chosen_objects SELECT p.object_id FROM pieces p JOIN streams s ON p.stream_id=s.id WHERE s.recipe_id=?',(selected,))
    db.commit()


def _materialize(pool,path,policy,average):
    db = pool.db
    _select(db,policy)
    output = _new_database(path)
    output.close()
    db.execute('ATTACH DATABASE ? AS candidate',(str(path),))
    try:
        db.execute('INSERT INTO candidate.files SELECT * FROM main.files')
        db.execute('INSERT INTO candidate.recipes SELECT r.* FROM main.recipes r JOIN selected_recipes s ON r.id=s.id')
        db.execute('INSERT INTO candidate.streams SELECT s.* FROM main.streams s JOIN selected_recipes r ON s.recipe_id=r.id')
        db.execute('INSERT INTO candidate.pieces SELECT p.* FROM main.pieces p JOIN candidate.streams s ON p.stream_id=s.id')
        db.execute('INSERT INTO candidate.objects SELECT o.* FROM main.objects o JOIN chosen_objects c ON o.id=c.id')
        settings = {'strategy':policy,'average':average,'external_codecs_are_lossless_dependencies':True}
        db.executemany('INSERT INTO candidate.meta VALUES(?,?)',[('version',VERSION),('complete','yes'),('settings',_json(settings))])
        db.commit()
    finally:
        db.execute('DETACH DATABASE candidate')
    final = sqlite3.connect(path)
    try:
        final.execute('VACUUM')
    finally:
        final.close()


def _check_structure(db,codecs):
    if db.execute('SELECT count(*) FROM meta').fetchone()[0]!=3:
        raise ArchiveError('Invalid metadata row count')
    if db.execute("SELECT 1 FROM meta WHERE typeof(key)<>'text' OR typeof(value)<>'text' OR length(CAST(key AS BLOB))>64 OR length(CAST(value AS BLOB))>8192 LIMIT 1").fetchone():
        raise ArchiveError('Invalid metadata fields or size')
    meta = dict(db.execute('SELECT key,value FROM meta'))
    if set(meta)!={'version','complete','settings'} or meta['version']!=VERSION or meta['complete']!='yes':
        raise ArchiveError('Unsupported or incomplete archive')
    settings = _parse(meta['settings'])
    if type(settings) is not dict or set(settings)!={'strategy','average','external_codecs_are_lossless_dependencies'}:
        raise ArchiveError('Invalid archive settings')
    if settings['strategy'] not in ('raw','native','semantic','hybrid') or settings['external_codecs_are_lossless_dependencies'] is not True:
        raise ArchiveError('Invalid strategy settings')
    average = _int(settings['average'],16384,1048576,'average chunk size')
    if average & (average-1):
        raise ArchiveError('Average chunk size must be a power of two')
    counts = {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in SCHEMA}
    if not 1<=counts['files']<=MAX_FILES or counts['recipes']!=counts['files'] or counts['objects']>MAX_OBJECTS or counts['pieces']>MAX_PIECES or counts['streams']>MAX_FILES*32:
        raise ArchiveError('Archive row limits or recipe counts are invalid')
    if db.execute("SELECT 1 FROM files WHERE typeof(id)<>'integer' OR typeof(size)<>'integer' OR typeof(path)<>'text' OR typeof(sha256)<>'text' OR typeof(info)<>'text' OR length(path)>220 OR length(sha256)<>64 OR length(CAST(info AS BLOB))>8192 LIMIT 1").fetchone():
        raise ArchiveError('Invalid file metadata types or lengths')
    metadata_bytes = db.execute('SELECT coalesce(sum(length(CAST(path AS BLOB))+length(CAST(info AS BLOB))+length(CAST(sha256 AS BLOB))+128),0) FROM files').fetchone()[0]
    if metadata_bytes>32*1024*1024:
        raise ArchiveError('File metadata exceeds the 32 MiB memory budget')
    files = db.execute('SELECT * FROM files ORDER BY id').fetchall()
    validate_names([f[1] for f in files])
    total = 0
    known_streams = set()
    for file_id,name,size,digest,info_text in files:
        _int(file_id,1,2**63-1,'file ID')
        total += _int(size,0,MAX_TOTAL,'file size')
        _digest(digest)
        info = _parse(info_text)
        if info is not None:
            try:
                validate_info(info,size)
            except (ValueError,TypeError,OverflowError) as exc:
                raise ArchiveError('Invalid audio layout metadata') from exc
        recipes = db.execute('SELECT id,kind FROM recipes WHERE file_id=?',(file_id,)).fetchall()
        if len(recipes)!=1 or recipes[0][1] not in ('raw','native','semantic'):
            raise ArchiveError('Invalid file recipe')
        recipe,kind = recipes[0]
        _int(recipe,1,2**63-1,'recipe ID')
        streams = db.execute('SELECT id,role,channel,offset,size FROM streams WHERE recipe_id=? ORDER BY id LIMIT 33',(recipe,)).fetchall()
        for stream,role,channel,offset,stream_size in streams:
            _int(stream,1,2**63-1,'stream ID')
            _int(channel,-1,7,'stream channel')
            _int(offset,0,size,'stream offset')
            _int(stream_size,0,size,'stream size')
        expected = [('raw',-1,0,size)] if kind=='raw' else None
        if expected is None:
            if info is None:
                raise ArchiveError('Audio recipe without recognized format')
            expected = [('raw',-1,o,n) for o,n in raw_spans(info,size)] + [('channel',c,0,channel_size(info)) for c in range(info['channels'])]
        if len(streams)!=len(expected) or sorted(tuple(s[1:]) for s in streams)!=sorted(expected):
            raise ArchiveError('Stream layout does not exactly cover original file')
        for stream,role,channel,offset,stream_size in streams:
            _int(stream,1,2**63-1,'stream ID')
            known_streams.add(stream)
            actual = ordinal = 0
            for position,obj,transform,params_text,piece_size in db.execute('SELECT ordinal,object_id,transform,params,size FROM pieces WHERE stream_id=? ORDER BY ordinal',(stream,)):
                if position!=ordinal:
                    raise ArchiveError('Non-contiguous piece order')
                ordinal += 1
                actual += _int(piece_size,1,4*1024*1024,'piece size')
                _int(obj,1,2**63-1,'object reference')
                params = _parse(params_text)
                if transform=='raw':
                    if params!={}:
                        raise ArchiveError('Raw piece has unexpected parameters')
                elif transform not in ('pcm','dsd') or role!='channel' or kind!='semantic' or transform!=info['kind'] or type(params) is not dict:
                    raise ArchiveError('Invalid transform context')
                row = db.execute('SELECT size FROM objects WHERE id=?',(obj,)).fetchone()
                if row is None:
                    raise ArchiveError('Missing object reference')
                if transform=='raw' and row[0]!=piece_size:
                    raise ArchiveError('Raw piece/object size mismatch')
                if actual>stream_size:
                    raise ArchiveError('Pieces exceed stream length')
            if actual!=stream_size:
                raise ArchiveError('Incomplete stream')
    if total>MAX_TOTAL:
        raise ArchiveError('Total decoded size exceeds format limit')
    if len(known_streams)!=counts['streams']:
        raise ArchiveError('Unreferenced or foreign stream')
    if db.execute('SELECT 1 FROM pieces p LEFT JOIN streams s ON p.stream_id=s.id WHERE s.id IS NULL LIMIT 1').fetchone():
        raise ArchiveError('Unreferenced piece')
    if db.execute('SELECT 1 FROM objects o WHERE NOT EXISTS(SELECT 1 FROM pieces p WHERE p.object_id=o.id) LIMIT 1').fetchone():
        raise ArchiveError('Unreferenced object')
    for size,codec,width,encoded_length in db.execute('SELECT size,codec,width,length(data) FROM objects'):
        _int(size,0,MAX_OBJECT,'object size')
        _int(encoded_length,0,MAX_OBJECT+65536,'encoded size')
        _int(width,1,4,'object width')
        if codec not in ('raw','zlib','lzma','flac','wavpack-dsd'):
            raise ArchiveError('Unsupported object codec')
        codecs.require(codec)
    return files,settings,counts,total


def _restore_file(db,entry,path,codecs):
    file_id,name,size,digest,info_text = entry
    info = _parse(info_text)
    recipe = db.execute('SELECT id FROM recipes WHERE file_id=?',(file_id,)).fetchone()[0]
    with path.open('x+b') as output:
        output.truncate(size)
        for stream,role,channel,offset,stream_size in db.execute('SELECT id,role,channel,offset,size FROM streams WHERE recipe_id=? ORDER BY id',(recipe,)):
            position = 0
            for obj,transform,params_text,piece_size in db.execute('SELECT object_id,transform,params,size FROM pieces WHERE stream_id=? ORDER BY ordinal',(stream,)):
                row = db.execute('SELECT * FROM objects WHERE id=?',(obj,)).fetchone()
                payload = _object_bytes(row,codecs)
                params = _parse(params_text)
                try:
                    if transform=='pcm':
                        data = restore_pcm(payload,params,info['width'])
                    elif transform=='dsd':
                        data = restore_dsd(payload,params)
                    else:
                        data = payload
                except (ValueError,TypeError,OverflowError) as exc:
                    raise ArchiveError('Invalid reversible-transform metadata') from exc
                if len(data)!=piece_size or position+len(data)>stream_size:
                    raise ArchiveError('Unexpected transform expansion')
                if role=='raw':
                    output.seek(offset+position)
                    output.write(data)
                else:
                    write_channel(output,info,channel,position,data)
                position += len(data)
        output.flush()
        os.fsync(output.fileno())
    if path.stat().st_size!=size or file_sha(path)!=digest:
        raise ArchiveError(f'Restored file checksum mismatch: {name}')


def _same_files(first,second):
    with first.open('rb') as a,second.open('rb') as b:
        while True:
            x,y = a.read(IO_BLOCK),b.read(IO_BLOCK)
            if x!=y:
                return False
            if not x:
                return True


def verify(archive,compare_sources=None,work_dir=None):
    codecs = Codecs(external=False)
    with _read_database(archive) as db:
        files,settings,counts,total = _check_structure(db,codecs)
        with tempfile.TemporaryDirectory(prefix='mastervault-verify-',dir=work_dir) as temporary:
            folder = Path(temporary)
            for index,entry in enumerate(files):
                restored = folder/f'{index}.restore'
                _restore_file(db,entry,restored,codecs)
                if compare_sources is not None:
                    if entry[1] not in compare_sources or not _same_files(restored,Path(compare_sources[entry[1]])):
                        raise ArchiveError('Restored bytes differ from current source')
                restored.unlink()
    return {'verified':True,'files':len(files),'original_bytes':total,'archive_bytes':Path(archive).stat().st_size,
            'strategy':settings['strategy'],'objects':counts['objects'],'pieces':counts['pieces']}


def inspect_archive(archive):
    with _read_database(archive) as db:
        files,settings,counts,total = _check_structure(db,Codecs(external=False))
        return {'format':VERSION,'structural_checks_passed':True,'content_verified':False,
                'archive_bytes':Path(archive).stat().st_size,'original_bytes':total,'strategy':settings['strategy'],
                'objects':counts['objects'],'pieces':counts['pieces'],'references_reused':counts['pieces']-counts['objects'],
                'codecs':dict(db.execute('SELECT codec,count(*) FROM objects GROUP BY codec')),
                'selected_recipes':dict(db.execute('SELECT kind,count(*) FROM recipes GROUP BY kind')),
                'files':[{'path':e[1],'size':e[2],'sha256':e[3],'format':_parse(e[4])} for e in files]}


def unpack(archive,output):
    destination = Path(output).absolute()
    no_links(destination)
    if destination.exists():
        raise ArchiveError('Restore requires a new destination directory')
    codecs = Codecs(external=False)
    with _read_database(archive) as db:
        files,_,_,total = _check_structure(db,codecs)
        destination.parent.mkdir(parents=True,exist_ok=True)
        no_links(destination.parent)
        if shutil.disk_usage(destination.parent).free<total+1024*1024:
            raise ArchiveError('Insufficient free space for full restoration')
        staging = Path(tempfile.mkdtemp(prefix='.mastervault-restore-',dir=destination.parent))
        try:
            for entry in files:
                target = staging.joinpath(*entry[1].split('/'))
                target.parent.mkdir(parents=True,exist_ok=True)
                _restore_file(db,entry,target,codecs)
            if destination.exists():
                raise ArchiveError('Restore destination appeared during verification')
            staging.rename(destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {'verified':True,'restored_files':len(files),'original_bytes':total,'output':str(destination)}


def _publish(source,destination):
    # Same-directory complete temporary copy; exclusive link cannot overwrite.
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent,prefix='.mastervault-',suffix='.tmp',delete=False) as out:
            temp = Path(out.name)
            with source.open('rb') as inp:
                shutil.copyfileobj(inp,out,IO_BLOCK)
            out.flush()
            os.fsync(out.fileno())
        os.link(temp,destination)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def pack(sources,output,average=65536,mode='auto',allow_growth=False,codecs='auto',work_dir=None):
    _int(average,16384,1048576,'average chunk size')
    if average & (average-1):
        raise ArchiveError('Average chunk size must be a power of two')
    if mode not in ('auto','raw','native','semantic','hybrid') or codecs not in ('auto','stdlib'):
        raise ArchiveError('Invalid archive mode/codecs setting')
    destination = Path(output).absolute()
    no_links(destination)
    if destination.exists():
        raise ArchiveError('Archive destination already exists')
    inputs = collect_sources(sources)
    destination.parent.mkdir(parents=True,exist_ok=True)
    no_links(destination.parent)
    if work_dir is not None:
        work_dir = Path(work_dir).absolute()
        no_links(work_dir)
        work_dir.mkdir(parents=True,exist_ok=True)
    codec_manager = Codecs(external=codecs=='auto')
    strategies = ('raw',) if mode=='raw' else ('raw','native','semantic')
    candidates = ('raw','native','semantic','hybrid') if mode=='auto' else (mode,)
    original_total = 0
    with tempfile.TemporaryDirectory(prefix='mastervault-build-',dir=work_dir) as temp:
        folder = Path(temp)
        pool = Pool(folder/'pool.sqlite',codec_manager)
        try:
            for file_id,(name,path) in enumerate(inputs,1):
                before = _snapshot(path)
                size = before[0]
                original_total += size
                digest = file_sha(path)
                info = probe(path)
                if info is not None:
                    validate_info(info,size)
                pool.db.execute('INSERT INTO files VALUES(?,?,?,?,?)',(file_id,name,size,digest,_json(info)))
                pool.file(file_id,path,info,average,strategies)
                if _snapshot(path)!=before:
                    raise ArchiveError(f'Source changed while packing: {name}')
            sizes = {}
            for policy in candidates:
                target = folder/f'{policy}.mva'
                _materialize(pool,target,policy,average)
                sizes[policy] = target.stat().st_size
        finally:
            pool.db.close()
        selected = min(sizes,key=sizes.get)
        selected_path = folder/f'{selected}.mva'
        result = verify(selected_path,dict(inputs),work_dir=folder)
        details = inspect_archive(selected_path)
        saving = original_total-selected_path.stat().st_size
        result.update({'candidate_bytes':sizes,'net_saving_bytes':saving,
                       'net_saving_percent':round(100*saving/original_total,3) if original_total else None,
                       'selected_recipes':details['selected_recipes'],'codecs':details['codecs'],
                       'references_reused':details['references_reused'],
                       'codec_tools':codec_manager.available(),'rejected_external_candidates':codec_manager.rejected_external_candidates,
                       'streaming':True,'average':average})
        if saving<=0 and not allow_growth:
            result['status'] = 'skipped_no_net_savings'
        else:
            _publish(selected_path,destination)
            result.update(status='created',archive=str(destination))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='MasterVault: byte-exact HiRes PCM and native DSD archive')
    sub = parser.add_subparsers(dest='command',required=True)
    create = sub.add_parser('pack')
    create.add_argument('sources',nargs='+',type=Path)
    create.add_argument('-o','--output',required=True,type=Path)
    create.add_argument('--average',type=int,default=65536)
    create.add_argument('--mode',choices=('auto','raw','native','semantic','hybrid'),default='auto')
    create.add_argument('--codecs',choices=('auto','stdlib'),default='auto')
    create.add_argument('--allow-growth',action='store_true')
    create.add_argument('--work-dir',type=Path)
    for command in ('inspect','verify','unpack'):
        item = sub.add_parser(command)
        item.add_argument('archive',type=Path)
        if command=='unpack':
            item.add_argument('-o','--output',required=True,type=Path)
        if command=='verify':
            item.add_argument('--work-dir',type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command=='pack':
            result = pack(args.sources,args.output,args.average,args.mode,args.allow_growth,args.codecs,args.work_dir)
        elif args.command=='verify':
            result = verify(args.archive,work_dir=args.work_dir)
        elif args.command=='unpack':
            result = unpack(args.archive,args.output)
        else:
            result = inspect_archive(args.archive)
        print(json.dumps(result,ensure_ascii=True,indent=2))
        return 0
    except (ArchiveError,CodecError,OSError,ValueError,sqlite3.Error) as exc:
        print(f'MasterVault: {exc}',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
