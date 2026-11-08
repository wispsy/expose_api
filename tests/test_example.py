import runpy
import unittest
from pathlib import Path

from asgi_client import request


class ExampleTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_handlers_example(self):
        example = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples" / "http_handlers.py"))
        app = example["app"]
        self.assertEqual((await request(app, '/healthz')).json, {'ready': True})
        result = await request(app, '/api/search', query=b'q=caf%C3%A9')
        self.assertEqual(result.json, {'query': 'café', 'results': [], 'health': '/healthz'})
        self.assertEqual((await request(app, '/api/search', query=b'q=one&q=two')).status, 400)

    async def test_notes_example(self):
        example = runpy.run_path(str(Path(__file__).resolve().parents[1] / "examples" / "notes.py"))
        created = await request(example["app"], method="POST", data={"title": "First note"})
        self.assertEqual(created.status, 201)
        self.assertEqual(created.json["content"], "")
        self.assertEqual((await request(example["app"])).json, [created.json])
        self.assertEqual((await example["notes"].get(created.json["id"])).title, "First note")
