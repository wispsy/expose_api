"""A bounded complete-body multipart/form-data profile with owned spooling."""

import re
import tempfile
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, fields

from .errors import ConfigurationError
from .files import _io
from .http_protocol import _HTTPError
from .requests import MultiDict

_TOKEN = rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_PARAMETER = re.compile(rb';[ \t]*(' + _TOKEN + rb')[ \t]*=[ \t]*(' + _TOKEN
                        + rb'|"(?:[^"\\\x00-\x1f\x7f]|\\[\x20-\x7e])*")[ \t]*')


def _invalid():
    return _HTTPError(400, 'multipart', 'Invalid multipart form data.')


def _limit():
    return _HTTPError(413, 'multipart_limit', 'Multipart form exceeds the selected limits.')


def _parameters(raw):
    head, separator, _ = raw.partition(b';')
    if any(value < 32 or value == 127 for value in raw):
        raise _invalid()
    try:
        selected = head.strip().decode('ascii').lower()
    except UnicodeError:
        raise _invalid() from None
    parameters, position = {}, len(head)
    while position < len(raw):
        matched = _PARAMETER.match(raw, position)
        if matched is None:
            raise _invalid()
        name, value = matched.groups()
        name = name.decode('ascii').lower()
        if name in parameters:
            raise _invalid()
        if value.startswith(b'"'):
            value = re.sub(rb'\\(.)', rb'\1', value[1:-1])
        parameters[name] = value
        position = matched.end()
    return selected, parameters


@dataclass(frozen=True, slots=True)
class MultipartLimits:
    max_total_bytes: int = 8 * 1024 * 1024
    max_part_bytes: int = 8 * 1024 * 1024
    max_field_bytes: int = 64 * 1024
    max_header_bytes: int = 8192
    max_parts: int = 64
    max_files: int = 16
    max_fields: int = 64
    spool_threshold: int = 256 * 1024

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            count = field.name in ('max_parts', 'max_files', 'max_fields')
            maximum = 1024 if count else 65536 if field.name == 'max_header_bytes' else 64 * 1024 * 1024
            minimum = 0 if field.name in ('max_files', 'max_fields') else 1
            if type(value) is not int or not minimum <= value <= maximum:
                raise ConfigurationError('Multipart limits must be bounded ordinary integers.')


class UploadFile:
    """Untrusted filename metadata plus an owned bounded temporary file."""

    def __init__(self, file, *, filename, content_type, size):
        self._file = file
        self.filename, self.content_type, self.size = filename, content_type, size

    @property
    def closed(self):
        return self._file.closed

    async def read(self, size=-1):
        if type(size) is not int or size < -1:
            raise ConfigurationError('Upload read size must be -1 or a nonnegative integer.')
        return await _io(self._file.read, size)

    async def seek(self, offset):
        if type(offset) is not int or offset < 0:
            raise ConfigurationError('Upload offsets must be nonnegative integers.')
        return await _io(self._file.seek, offset)

    async def aclose(self):
        if not self.closed:
            await _io(self._file.close)

    async def close(self):
        await self.aclose()


class MultipartForm(MultiDict):
    def __init__(self, pairs, files):
        super().__init__(pairs)
        self._files = tuple(files)

    async def aclose(self):
        async with AsyncExitStack() as stack:
            for file in self._files:
                stack.push_async_callback(file.aclose)


def _parts(body, boundary, limits):
    opening = b'--' + boundary
    if body in (opening + b'--', opening + b'--\r\n'):
        return
    if not body.startswith(opening + b'\r\n'):
        raise _invalid()
    delimiter = re.compile(rb'\r\n--' + re.escape(boundary) + rb'(?:(--)(?:\r\n|\Z)|\r\n)')
    position, count = len(opening) + 2, 0
    while True:
        end = body.find(b'\r\n\r\n', position)
        if end < 0:
            if len(body) - position > limits.max_header_bytes:
                raise _limit()
            raise _invalid()
        if end - position > limits.max_header_bytes:
            raise _limit()
        headers = {}
        for line in body[position:end].split(b'\r\n'):
            name, separator, value = line.partition(b':')
            if not separator or re.fullmatch(_TOKEN, name) is None:
                raise _invalid()
            name, value = name.lower(), value.strip(b' \t')
            if (name in headers or name not in (b'content-disposition', b'content-type')
                    or any(byte < 32 or byte == 127 for byte in value)):
                raise _invalid()
            headers[name] = value
        content_start = end + 4
        matched = delimiter.search(body, content_start)
        if matched is None:
            raise _invalid()
        count += 1
        if count > limits.max_parts or matched.start() - content_start > limits.max_part_bytes:
            raise _limit()
        yield headers, body[content_start:matched.start()]
        if matched[1]:
            if matched.end() != len(body):
                raise _invalid()
            return
        position = matched.end()


async def _parse(request, limits):
    raw_type = request.header('content-type')
    if raw_type is None:
        raise _HTTPError(415, 'media_type', 'A multipart/form-data Content-Type is required.')
    if len(raw_type) > limits.max_header_bytes:
        raise _limit()
    media, parameters = _parameters(raw_type.encode('latin-1'))
    if media != 'multipart/form-data':
        raise _HTTPError(415, 'media_type', 'A multipart/form-data Content-Type is required.')
    if set(parameters) != {'boundary'} or not re.fullmatch(rb"[-0-9A-Za-z'()+_,./:=?]{1,70}", parameters['boundary']):
        raise _invalid()
    body = await request.body(max_size=limits.max_total_bytes)
    pairs, files, field_count = [], [], 0
    try:
        for headers, data in _parts(body, parameters['boundary'], limits):
            disposition, options = _parameters(headers.get(b'content-disposition', b''))
            if disposition != 'form-data' or 'name' not in options or set(options) - {'name', 'filename'}:
                raise _invalid()
            try:
                name = options['name'].decode('utf-8')
                filename = options['filename'].decode('utf-8') if 'filename' in options else None
            except UnicodeError:
                raise _invalid() from None
            if (not name or len(name) > 256 or filename is not None and len(filename) > 1024
                    or any(ord(c) < 32 or ord(c) == 127 for c in name + (filename or ''))):
                raise _invalid()
            content_type = headers.get(b'content-type', b'application/octet-stream' if filename is not None else b'text/plain')
            selected_type, type_options = _parameters(content_type)
            if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+", selected_type)
                    or selected_type.startswith('multipart/')
                    or set(type_options) - {'charset'} or type_options.get('charset', b'utf-8').lower() not in (b'utf-8', b'utf8')):
                raise _invalid()
            if filename is None:
                field_count += 1
                if field_count > limits.max_fields or len(data) > limits.max_field_bytes:
                    raise _limit()
                if selected_type != 'text/plain':
                    raise _invalid()
                try:
                    value = data.decode('utf-8')
                except UnicodeError:
                    raise _invalid() from None
            else:
                if len(files) >= limits.max_files:
                    raise _limit()
                file = tempfile.SpooledTemporaryFile(max_size=limits.spool_threshold, mode='w+b')
                value = UploadFile(file, filename=filename, content_type=selected_type, size=len(data))
                files.append(value)
                await _io(file.write, data)
                await _io(file.seek, 0)
            pairs.append((name, value))
        return MultipartForm(pairs, files)
    except BaseException:
        await MultipartForm((), files).aclose()
        raise


@asynccontextmanager
async def multipart(request, *, limits=None):
    limits = MultipartLimits() if limits is None else limits
    if not isinstance(limits, MultipartLimits):
        raise ConfigurationError('Select MultipartLimits explicitly.')
    form = await _parse(request, limits)
    try:
        yield form
    finally:
        await form.aclose()
