"""Owned regular-file responses with bounded reads and HTTP validators."""

import asyncio
import mimetypes
import os
import re
import stat
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

from .errors import ConfigurationError
from .requests import HTTPRequest
from .responses import HTTPException, Response, _headers
from .streaming import StreamingResponse


async def _io(function, *args):
    """Finish an in-flight file operation before its descriptor can be closed."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


def _open_file(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        return os.fdopen(descriptor, 'rb', buffering=0)
    except BaseException:
        os.close(descriptor)
        raise


class _FileChunks:
    def __init__(self, file, count, chunk_size):
        self.file, self.remaining, self.chunk_size = file, count, chunk_size

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.remaining:
            raise StopAsyncIteration
        data = await _io(self.file.read, min(self.remaining, self.chunk_size))
        if not data:
            raise StopAsyncIteration
        self.remaining -= len(data)
        return data

    async def aclose(self):
        if not self.file.closed:
            await _io(self.file.close)


def _date(value):
    try:
        if value is None or len(value) > 128:
            return None
        parsed = parsedate_to_datetime(value)
        return int(parsed.timestamp()) if parsed.utcoffset() is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _unchanged(context, etag, modified):
    none_match = context.header('if-none-match')
    if none_match is not None:
        if none_match.strip() == '*':
            return True
        if len(none_match) > 8192:
            return False
        tag = r'(?:W/)?"[^"\x00-\x20\x7f]*"'
        if not re.fullmatch(r'\s*' + tag + r'(?:\s*,\s*' + tag + r')*\s*', none_match):
            return False
        values = re.findall(tag, none_match)
        if len(values) > 128:
            return False
        return etag.removeprefix('W/') in [value.removeprefix('W/') for value in values]
    since = _date(context.header('if-modified-since'))
    return since is not None and modified <= since


def _range(value, size):
    """The selected profile supports one byte range; other units are ignored."""
    if value is None or not value.startswith('bytes='):
        return None
    matched = re.fullmatch(r'bytes=([0-9]{0,20})-([0-9]{0,20})', value)
    if matched is None or not any(matched.groups()) or size == 0:
        raise ValueError
    first, last = matched.groups()
    if first:
        start, end = int(first), min(int(last), size - 1) if last else size - 1
        if start >= size or end < start:
            raise ValueError
    else:
        suffix = int(last)
        if suffix == 0:
            raise ValueError
        start, end = max(0, size - suffix), size - 1
    return start, end


class FileResponse(Response):
    """Prepare one explicitly selected regular file; static rooting is separate."""

    def __init__(self, path, *, filename=None, media_type=None, headers=(), chunk_size=65536, _opener=None):
        self.path = Path(path)
        headers = _headers(headers)
        if any(name in (b'content-length', b'content-range', b'etag', b'last-modified', b'accept-ranges') for name, _ in headers):
            raise ConfigurationError('File metadata and framing are owned by FileResponse.')
        if type(chunk_size) is not int or not 1 <= chunk_size <= 1024 * 1024:
            raise ConfigurationError('File chunks must be between 1 byte and 1 MiB.')
        if filename is not None:
            if (type(filename) is not str or not filename or len(filename) > 255
                    or any(c in '/\\' or ord(c) < 32 or ord(c) == 127 for c in filename)):
                raise ConfigurationError('Download names must be safe single filenames.')
            fallback = re.sub(r'[^A-Za-z0-9._ -]', '_', filename)
            headers.extend(_headers((('content-disposition', 'attachment; filename="' + fallback
                                      + '"; filename*=UTF-8\'\'' + quote(filename, safe='')),)))
        selected_type = media_type or mimetypes.guess_file_type(self.path)[0] or 'application/octet-stream'
        super().__init__(headers=headers, media_type=selected_type)
        self.headers = [(name, value) for name, value in self.headers if name != b'content-length']
        self.chunk_size, self._opener, self._used = chunk_size, _opener or _open_file, False

    async def prepare(self, scope):
        if self._used:
            raise ConfigurationError('File responses are single-use.')
        self._used = True
        opening = asyncio.create_task(asyncio.to_thread(self._opener, self.path))
        try:
            file = await asyncio.shield(opening)
        except asyncio.CancelledError:
            try:
                file = await opening
            except Exception:
                pass
            else:
                await _io(file.close)
            raise
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
            raise HTTPException(404, 'File not found.') from None
        transferred = False
        try:
            info = await _io(os.fstat, file.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise HTTPException(404, 'File not found.')
            size, modified = info.st_size, int(info.st_mtime)
            etag = f'W/"{info.st_mtime_ns:x}-{size:x}"'
            headers = [*self.headers, (b'etag', etag.encode('ascii')), (b'last-modified', formatdate(modified, usegmt=True).encode('ascii')),
                       (b'accept-ranges', b'bytes')]
            context = HTTPRequest(scope, None)
            if scope['method'] in ('GET', 'HEAD') and _unchanged(context, etag, modified):
                return Response(status=304, headers=headers)
            selected_range = None
            if scope['method'] == 'GET':
                value = context.header('range')
                if_range = context.header('if-range')
                if if_range is not None:
                    date = _date(if_range)
                    if date is None or date < modified:
                        value = None
                try:
                    selected_range = _range(value, size)
                except ValueError:
                    return Response(status=416, headers=[*headers, (b'content-range', f'bytes */{size}'.encode('ascii'))])
            status, start, count = 200, 0, size
            if selected_range is not None:
                start, end = selected_range
                status, count = 206, end - start + 1
                headers.append((b'content-range', f'bytes {start}-{end}/{size}'.encode('ascii')))
            await _io(file.seek, start)
            response = StreamingResponse(_FileChunks(file, count, self.chunk_size), status=status,
                                         headers=headers, content_length=count, chunk_size=self.chunk_size)
            transferred = True
            return response
        finally:
            if not transferred:
                await _io(file.close)

    async def __call__(self, scope, receive, send):
        response = await self.prepare(scope)
        await response(scope, receive, send)
