"""Qualify an already installed caller-owned Jinja engine; never install one."""

import asyncio
import importlib.metadata

from expose_api import App, ConfigurationError, HTTPRequest, JinjaTemplates, Response


async def main():
    from jinja2 import DictLoader, Environment, select_autoescape
    app = App()
    app.route('/items/{name}', public=True, name='item')(lambda request: Response())
    request = HTTPRequest({'method': 'GET', 'path': '/page', 'root_path': '/root'}, None, app=app)
    for asynchronous in (False, True):
        environment = Environment(loader=DictLoader({'page.html': '<h1>{{ title }}</h1><a href="{{ url_for("item", name="café") }}">item</a>'}),
                                  autoescape=select_autoescape(), enable_async=asynchronous)
        response = await JinjaTemplates(environment).response(request, 'page.html', {'title': '<script>private</script>'})
        assert b'&lt;script&gt;private&lt;/script&gt;' in response.body
        assert b'/root/items/caf%C3%A9' in response.body
        assert b'<script>' not in response.body
    try:
        JinjaTemplates(Environment(autoescape=False))
    except ConfigurationError:
        pass
    else:
        raise AssertionError('An unescaped environment was accepted.')
    print('Caller-owned Jinja', importlib.metadata.version('jinja2'), 'passed synchronous/asynchronous escaping and local URL composition.')


if __name__ == '__main__':
    asyncio.run(main())
