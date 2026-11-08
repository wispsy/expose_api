import asyncio
import html
import unittest

from expose_api import App, ConfigurationError, HTTPRequest, JinjaTemplates, Response, TemplateRenderer
from asgi_client import request


class EnvironmentFixture:
    def __init__(self, *, asynchronous=False, autoescape=True):
        self.autoescape, self.is_async, self.loaded = autoescape, asynchronous, []

    def get_template(self, name):
        self.loaded.append(name)
        environment = self
        class Template:
            def render(self, **context):
                return '<p>' + html.escape(context['title']) + '</p>'
            async def render_async(self, **context):
                await asyncio.sleep(0)
                return self.render(**context)
        template = Template()
        template.environment = environment
        return template


class TemplateTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_copy_request_and_named_local_urls(self):
        app, seen = App(), []
        app.mount('/assets', App(), name='assets')
        original = {'title': '<script>hello</script>'}
        def render(name, context):
            seen.append((name, context['request'].path))
            context['added'] = True
            return '<h1>' + html.escape(context['title']) + '</h1><a href="' + context['url_for']('assets', path='café.css') + '">asset</a>'
        templates = TemplateRenderer(render)
        @app.route('/page', public=True)
        async def page(context):
            return await templates.response(context, 'home/page.html', original, status=201, headers={'x-page': 'yes'})
        result = await request(app, '/root/page', root_path='/root', headers=[(b'host', b'untrusted.example')])
        self.assertEqual((result.status, result.headers[b'x-page']), (201, b'yes'))
        self.assertIn(b'&lt;script&gt;hello&lt;/script&gt;', result.body)
        self.assertIn(b'/root/assets/caf%C3%A9.css', result.body)
        self.assertNotIn('added', original)
        self.assertEqual(seen, [('home/page.html', '/root/page')])

    async def test_async_renderer_and_jinja_environment_modes(self):
        context = HTTPRequest({'method': 'GET', 'path': '/'}, None, app=App())
        for asynchronous in (False, True):
            environment = EnvironmentFixture(asynchronous=asynchronous)
            templates = JinjaTemplates(environment)
            self.assertEqual(environment.loaded, [])
            result = await templates.response(context, 'page.html', {'title': '<tag>'})
            self.assertEqual(result.body, b'<p>&lt;tag&gt;</p>')
            self.assertEqual(environment.loaded, ['page.html'])
        async def render(name, values):
            await asyncio.sleep(0)
            return 'async result'
        self.assertEqual((await TemplateRenderer(render).response(context, 'page.html')).body, b'async result')

    async def test_autoescape_selection_and_environment_ownership(self):
        context = HTTPRequest({'method': 'GET', 'path': '/'}, None)
        environment = EnvironmentFixture(autoescape=lambda name: name.endswith('.html'))
        templates = JinjaTemplates(environment)
        with self.assertRaises(ConfigurationError):
            await templates.response(context, 'page.txt')
        with self.assertRaises(ConfigurationError):
            JinjaTemplates(EnvironmentFixture(autoescape=False))
        other = EnvironmentFixture()
        environment.get_template = other.get_template
        with self.assertRaises(ConfigurationError):
            await templates.response(context, 'page.html')

    async def test_invalid_names_context_and_output(self):
        context = HTTPRequest({'method': 'GET', 'path': '/'}, None)
        templates = TemplateRenderer(lambda name, values: 'ok')
        for name in ('/absolute', 'C:/outside.html', 'C:outside.html', '../parent', 'a/../parent', 'a\\b', 'a//b', 'bad\x00name'):
            with self.subTest(name=name), self.assertRaises(ConfigurationError):
                await templates.response(context, name)
        environment = EnvironmentFixture()
        for name in ('C:/outside.html', 'C:outside.html'):
            with self.assertRaises(ConfigurationError):
                await JinjaTemplates(environment).response(context, name)
        self.assertEqual(environment.loaded, [])
        for values in ({'request': 'override'}, {'url_for': 'override'}, {1: 'wrong'}):
            with self.assertRaises(ConfigurationError):
                await templates.response(context, 'page.html', values)
        with self.assertRaises(ConfigurationError):
            await TemplateRenderer(lambda name, values: b'bytes').response(context, 'page.html')

    async def test_renderer_errors_are_redacted_by_the_application(self):
        def render(name, values):
            raise RuntimeError('private token in loader failure')
        templates = TemplateRenderer(render)
        app = App()
        @app.route('/page', public=True)
        async def page(context):
            return await templates.response(context, 'private/template.html')
        with self.assertLogs('expose_api', 'ERROR') as logs:
            result = await request(app, '/page')
        self.assertEqual(result.status, 500)
        self.assertNotIn('private', result.body.decode() + str(logs.output))

    async def test_renderer_cancellation_propagates(self):
        started = asyncio.Event()
        async def render(name, context):
            started.set()
            await asyncio.Future()
        context = HTTPRequest({'method': 'GET', 'path': '/'}, None)
        task = asyncio.create_task(TemplateRenderer(render).response(context, 'page.html'))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
