#!/usr/bin/env python3
"""Validate, mirror and package the image-only repository contract."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import zipfile
import zlib

CATEGORIES = ('atlas', 'image', 'others')
MAX_TOTAL_BYTES = 750 * 1024 * 1024

def validate_png(stream):
    if stream.read(8) != b'\x89PNG\r\n\x1a\n':
        raise ValueError('Invalid PNG signature')
    first, data_seen = True, False
    while True:
        header = stream.read(8)
        if len(header) != 8:
            raise ValueError('Truncated PNG chunk')
        length, kind = struct.unpack('>I4s', header)
        if length > 64 * 1024 * 1024 or first and (kind != b'IHDR' or length != 13):
            raise ValueError('Invalid PNG chunk')
        payload = stream.read(length)
        crc = stream.read(4)
        if len(payload) != length or len(crc) != 4 or struct.unpack('>I', crc)[0] != zlib.crc32(kind + payload):
            raise ValueError('Truncated or corrupt PNG chunk')
        if first:
            width, height = struct.unpack('>II', payload[:8])
            if not width or not height or width * height > 100_000_000:
                raise ValueError('Invalid PNG dimensions')
        elif kind == b'IHDR':
            raise ValueError('Duplicate PNG header')
        first = False
        data_seen |= kind == b'IDAT'
        if kind == b'IEND':
            if length or not data_seen or stream.read(1):
                raise ValueError('Invalid PNG end')
            return
VERSION = re.compile(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z')
SAFE_COMPONENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._ -]{0,253}[A-Za-z0-9._-]\Z|[A-Za-z0-9]\Z')

def inventory(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Image repository root must be a real directory')
    version_path = root / 'texture_version.txt'
    if version_path.is_symlink() or not version_path.is_file() or version_path.stat().st_size > 32:
        raise ValueError('Missing or unsafe texture version')
    version = version_path.read_text(encoding='ascii').strip()
    if not VERSION.fullmatch(version):
        raise ValueError('Texture version must be major.minor.patch')
    files, names, total = {}, set(), 0
    for category in CATEGORIES:
        directory = root / category
        if directory.is_symlink():
            raise ValueError('Image category cannot be a symlink')
        if not directory.exists():
            continue
        if not directory.is_dir():
            raise ValueError('Image category must be a directory')
        for item in sorted(directory.rglob('*')):
            relative = item.relative_to(root).as_posix()
            if item.is_symlink() or any(not SAFE_COMPONENT.fullmatch(part) for part in item.relative_to(root).parts):
                raise ValueError('Unsafe image path: ' + relative)
            if relative.casefold() in names:
                raise ValueError('Case-insensitive image path collision')
            names.add(relative.casefold())
            for part in item.relative_to(root).parts:
                if part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(n) for n in range(1,10)), *('LPT'+str(n) for n in range(1,10))}:
                    raise ValueError('Reserved portable filename')
            if item.is_dir():
                if category == 'atlas':
                    raise ValueError('Atlas sprites must be composed before publishing')
                continue
            if not item.is_file() or item.suffix.lower() != '.png':
                raise ValueError('Only PNG artifacts may be published: ' + relative)
            size = item.stat().st_size
            if not 24 <= size <= 64 * 1024 * 1024:
                raise ValueError('Image size is outside the publication limit')
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ValueError('Approved image distribution exceeds the 750 MiB limit; split distribution before adding more images')
            with item.open('rb') as stream:
                validate_png(stream)
                stream.seek(0)
                digest = hashlib.file_digest(stream, 'sha256')
            files[relative] = {'bytes': size, 'sha256': digest.hexdigest()}
    if not files:
        raise ValueError('Empty image publication is refused')
    files['texture_version.txt'] = {'bytes': version_path.stat().st_size,
                                   'sha256': hashlib.sha256(version_path.read_bytes()).hexdigest()}
    return {'version': version, 'files': files, 'images': len(files) - 1}

def mirror(source, target):
    source, target = Path(source).resolve(), Path(target)
    if target.is_symlink() or not target.is_dir():
        raise ValueError('Target must be a real repository directory')
    target = target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError('Source and target must be separate directories')
    before = inventory(source)
    # Preserve repository workflows and metadata; own only the image contract.
    for category in CATEGORIES:
        destination = target / category
        if destination.is_symlink() or destination.exists() and not destination.is_dir():
            raise ValueError('Unsafe target category')
    version_path = target / 'texture_version.txt'
    if version_path.is_symlink() or version_path.exists() and not version_path.is_file():
        raise ValueError('Unsafe target version path')
    for category in CATEGORIES:
        destination = target / category
        if destination.exists():
            shutil.rmtree(destination)
        if (source / category).exists():
            shutil.copytree(source / category, destination)
    shutil.copyfile(source / 'texture_version.txt', version_path)
    after = inventory(target)
    if after != before or inventory(source) != before:
        raise ValueError('Image inventory changed while mirroring')
    return before

def package(root, output):
    root, output = Path(root), Path(output)
    before = inventory(root)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'texture2d.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for relative in sorted(before['files']):
            entry = zipfile.ZipInfo('texture2d/' + relative, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry._compresslevel = 6
            entry.external_attr = 0o100644 << 16
            with zipped.open(entry, 'w') as destination, (root / relative).open('rb') as source:
                shutil.copyfileobj(source, destination, 1024 * 1024)
    if inventory(root) != before:
        raise ValueError('Image inventory changed while packaging')
    with zipfile.ZipFile(archive) as zipped:
        if zipped.testzip() is not None:
            raise ValueError('Archive verification failed')
        for relative, expected in before['files'].items():
            with zipped.open('texture2d/' + relative) as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != expected['sha256']:
                raise ValueError('Archive bytes differ from source')
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    (output / 'SHA256SUMS').write_text(digest + '  texture2d.zip\n')
    return {'version': before['version'], 'images': before['images'], 'archive_sha256': digest}

def verify(root, archive):
    expected = inventory(root)
    with zipfile.ZipFile(archive) as zipped:
        names = ['texture2d/' + name for name in expected['files']]
        if len(zipped.infolist()) != len(names) or set(zipped.namelist()) != set(names):
            raise ValueError('Published version has a different image inventory; bump texture_version.txt')
        for path, wanted in expected['files'].items():
            entry = zipped.getinfo('texture2d/' + path)
            if entry.file_size != wanted['bytes']:
                raise ValueError('Published version has different content; bump texture_version.txt')
            with zipped.open(entry) as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != wanted['sha256']:
                raise ValueError('Published version has different content; bump texture_version.txt')
    return {'version': expected['version'], 'images': expected['images'], 'verified': True}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['inspect', 'mirror', 'package', 'verify'])
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path, nargs='?')
    args = parser.parse_args()
    if args.operation != 'inspect' and args.destination is None:
        parser.error('destination is required')
    result = inventory(args.source) if args.operation == 'inspect' else {'mirror': mirror, 'package': package, 'verify': verify}[args.operation](args.source, args.destination)
    print(json.dumps({key: value for key, value in result.items() if key != 'files'}))

if __name__ == '__main__':
    main()
