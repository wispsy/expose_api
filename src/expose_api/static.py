"""Rooted static assets from an explicitly selected, trusted directory tree."""

import os
import ntpath
import stat
from pathlib import Path

from .errors import ConfigurationError
from .files import FileResponse, _open_file
from .http_protocol import ProtocolError
from .responses import HTTPException, JSONResponse, Response, _headers

_RESERVED = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{prefix}{number}' for prefix in ('COM', 'LPT') for number in range(1, 10)}


def _parts(path, allow_hidden):
    try:
        path.encode('utf-8')
    except UnicodeError:
        raise HTTPException(404, 'Asset not found.') from None
    parts = path.removeprefix('/').split('/')
    if (len(path) > 4096 or len(parts) > 128 or any(
            part in ('', '.', '..') or part.endswith(('.', ' ')) or not allow_hidden and part.startswith('.')
            or part.split('.')[0].upper() in _RESERVED or ntpath.isreserved(part)
            or any(c in '\\<>:"|?*' or ord(c) < 32 or ord(c) == 127 for c in part) for part in parts)):
        raise HTTPException(404, 'Asset not found.')
    return tuple(parts)


def _reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))


def _open_rooted(root, parts):
    if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW') and hasattr(os, 'O_DIRECTORY'):
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=directory)
        finally:
            os.close(directory)
        try:
            return os.fdopen(descriptor, 'rb', buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
    # On platforms without directory descriptors, the selected tree and its
    # ancestors must remain trusted. Check stable reparse points and identity.
    selected = root
    for part in parts:
        selected = selected / part
        if _reparse(selected.lstat()):
            raise FileNotFoundError
    selected.resolve(strict=True).relative_to(root)
    before = selected.stat(follow_symlinks=False)
    file = _open_file(selected)
    try:
        after = selected.stat(follow_symlinks=False)
        actual = os.fstat(file.fileno())
        selected.resolve(strict=True).relative_to(root)
        if (_reparse(after) or (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino)
                or (after.st_dev, after.st_ino) != (actual.st_dev, actual.st_ino)):
            raise FileNotFoundError
        return file
    except BaseException:
        file.close()
        raise


class StaticFiles:
    """HTTP-only, no directory listing, redirect or implicit index selection."""

    def __init__(self, directory, *, headers=(), allow_hidden=False, chunk_size=65536):
        try:
            self.directory = Path(directory).resolve(strict=True)
        except (OSError, ValueError):
            raise ConfigurationError('Select an existing static directory.') from None
        if not self.directory.is_dir() or type(allow_hidden) is not bool:
            raise ConfigurationError('Select an existing directory and explicit hidden-file policy.')
        headers = _headers(headers)
        if any(name in (b'content-length', b'content-range', b'etag', b'last-modified', b'accept-ranges') for name, _ in headers):
            raise ConfigurationError('Static file metadata is owned by the response.')
        if not any(name == b'cache-control' for name, _ in headers):
            headers.append((b'cache-control', b'public, max-age=0, must-revalidate'))
        if not any(name == b'x-content-type-options' for name, _ in headers):
            headers.append((b'x-content-type-options', b'nosniff'))
        if type(chunk_size) is not int or not 1 <= chunk_size <= 1024 * 1024:
            raise ConfigurationError('Static chunks must be between 1 byte and 1 MiB.')
        self.headers, self.allow_hidden, self.chunk_size = headers, allow_hidden, chunk_size

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            raise ProtocolError('StaticFiles accepts HTTP scopes only.')
        try:
            method = scope['method']
            if method == 'OPTIONS':
                response = Response(status=204, headers={'allow': 'GET, HEAD, OPTIONS'})
            elif method not in ('GET', 'HEAD'):
                raise HTTPException(405, 'Method not allowed.', headers={'allow': 'GET, HEAD, OPTIONS'})
            else:
                root = scope.get('root_path', '').rstrip('/')
                path = scope['path']
                if root and (path == root or path.startswith(root + '/')):
                    path = path[len(root):] or '/'
                parts = _parts(path, self.allow_hidden)
                def opener(path):
                    try:
                        return _open_rooted(self.directory, parts)
                    except (OSError, ValueError):
                        raise FileNotFoundError from None
                response = await FileResponse(self.directory.joinpath(*parts), headers=self.headers,
                                              chunk_size=self.chunk_size, _opener=opener).prepare(scope)
        except HTTPException as exc:
            response = JSONResponse({'error': {'code': 'http_error', 'message': exc.detail, 'details': []}},
                                    status=exc.status, headers=exc.headers)
        await response(scope, receive, send)
