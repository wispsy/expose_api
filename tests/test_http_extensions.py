import asyncio
import hashlib
import json
import runpy
import tempfile
import unittest
from pathlib import Path

from expose_api import BearerAuth, Principal, serve
from asgi_client import request


def upload_body(payload):
    return (b'--Example\r\nContent-Disposition: form-data; name="file"; filename="untrusted.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\n' + payload + b'\r\n--Example--\r\n')


class HTTPExtensionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        directory = Path(self.work.name)
        (directory / 'site.css').write_bytes(b'body{color:blue}')
        (directory / 'download.txt').write_bytes(b'download content')
        example = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'examples' / 'http_extensions.py'))
        auth = BearerAuth(lambda token: Principal('fixture', scopes=frozenset({'upload'})) if token == 'example' else None)
        self.app = example['create_app'](directory, authenticate=auth)

    async def test_rooted_composition_protocols_and_documentation(self):
        page = await request(self.app, '/root/', root_path='/root', query=b'title=%3Cscript%3E')
        self.assertEqual(page.status, 200)
        self.assertIn(b'&lt;script&gt;', page.body)
        self.assertIn(b'/root/assets/site.css', page.body)
        self.assertIn(b'/root/upload', page.body)
        asset = await request(self.app, '/root/assets/site.css', root_path='/root', hold=True)
        self.assertEqual((asset.status, asset.body), (200, b'body{color:blue}'))
        download = await request(self.app, '/root/download', root_path='/root', headers=[(b'range', b'bytes=0-3')], hold=True)
        self.assertEqual((download.status, download.body), (206, b'down'))
        stream = await request(self.app, '/root/stream', root_path='/root', hold=True)
        self.assertEqual(stream.body, b'first\nsecond\n')
        self.assertTrue(self.app.state['stream_closed'])
        denied = await request(self.app, '/root/upload', 'POST', root_path='/root', body=b'bad')
        self.assertEqual((denied.status, denied.reads), (401, 0))
        payload = b'binary\x00\xff'
        uploaded = await request(self.app, '/root/upload', 'POST', root_path='/root', body=upload_body(payload), headers=[
            (b'content-type', b'multipart/form-data; boundary=Example'), (b'authorization', b'Bearer example')])
        self.assertEqual((uploaded.status, uploaded.headers[b'cache-control']), (201, b'no-store'))
        self.assertEqual(uploaded.json['sha256'], hashlib.sha256(payload).hexdigest())
        self.assertEqual(uploaded.json['subject'], 'fixture')
        self.assertTrue(self.app.state['upload_closed'])
        specification = (await request(self.app, '/root/openapi.json', root_path='/root')).json
        self.assertNotIn('/openapi.json', specification['paths'])
        self.assertEqual(specification['paths']['/upload']['post']['security'], [{'BearerAuth': []}])
        self.assertIn('multipart/form-data', specification['paths']['/upload']['post']['requestBody']['content'])

    async def test_real_http_installation_lifecycle_and_upload(self):
        async with serve(self.app, port=0) as server:
            self.assertTrue(self.app.state['running'])
            port = server.sockets[0].getsockname()[1]
            async def exchange(method, path, body=b'', headers=()):
                reader, writer = await asyncio.open_connection('127.0.0.1', port)
                try:
                    pairs = [('Host', 'localhost'), ('Content-Length', str(len(body))), *headers]
                    head = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{name}: {value}\r\n' for name, value in pairs) + '\r\n'
                    writer.write(head.encode('ascii') + body)
                    await writer.drain()
                    raw = await asyncio.wait_for(reader.read(), 5)
                finally:
                    writer.close()
                    await writer.wait_closed()
                head, _, content = raw.partition(b'\r\n\r\n')
                return int(head.split(b' ', 2)[1]), content
            status, content = await exchange('GET', '/healthz')
            self.assertEqual((status, json.loads(content)), (200, {'running': True}))
            self.assertEqual(await exchange('HEAD', '/assets/site.css'), (200, b''))
            self.assertEqual(await exchange('GET', '/download', headers=[('Range', 'bytes=0-3')]), (206, b'down'))
            self.assertEqual(await exchange('GET', '/stream'), (200, b'first\nsecond\n'))
            payload = b'real HTTP upload'
            status, content = await exchange('POST', '/upload', upload_body(payload), headers=[
                ('Content-Type', 'multipart/form-data; boundary=Example'), ('Authorization', 'Bearer example')])
            self.assertEqual((status, json.loads(content)['sha256']), (201, hashlib.sha256(payload).hexdigest()))
        self.assertFalse(self.app.state['running'])
        self.assertTrue(self.app.state['upload_closed'])
