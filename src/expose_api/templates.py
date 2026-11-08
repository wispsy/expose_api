"""Explicit HTML responses through caller-owned template renderers."""

import inspect
from collections.abc import Mapping

from .errors import ConfigurationError
from .requests import HTTPRequest
from .responses import HTMLResponse


def _name(name):
    if (type(name) is not str or not name or len(name) > 1024 or name.startswith('/')
            or any(c == '\\' or ord(c) < 32 or ord(c) == 127 for c in name)
            or any(part in ('', '.', '..') for part in name.split('/'))):
        raise ConfigurationError('Template names must be safe relative loader names.')
    try:
        name.encode('utf-8')
    except UnicodeError:
        raise ConfigurationError('Template names must be valid Unicode.') from None
    return name


class TemplateRenderer:
    """A trusted HTML renderer; the selected engine owns escaping and loading."""

    def __init__(self, render):
        if not callable(render):
            raise ConfigurationError('Select a callable template renderer.')
        self._render = render

    async def response(self, request, name, context=None, *, status=200, headers=()):
        name = _name(name)
        if not isinstance(request, HTTPRequest) or context is not None and not isinstance(context, Mapping):
            raise ConfigurationError('Template responses require an HTTPRequest and a context mapping.')
        values = dict(context or {})
        if any(type(key) is not str for key in values) or {'request', 'url_for'} & values.keys():
            raise ConfigurationError('Template context keys must be strings and preserve request/url_for.')
        values.update(request=request, url_for=request.url_for)
        content = self._render(name, values)
        if inspect.isawaitable(content):
            content = await content
        if not isinstance(content, str):
            raise ConfigurationError('HTML template renderers must return text.')
        return HTMLResponse(content, status=status, headers=headers)


class JinjaTemplates(TemplateRenderer):
    """A structural adapter for a caller-owned, autoescaping Jinja environment."""

    def __init__(self, environment):
        autoescape = getattr(environment, 'autoescape', None)
        if (not callable(getattr(environment, 'get_template', None))
                or autoescape is not True and not callable(autoescape)
                or type(getattr(environment, 'is_async', False)) is not bool):
            raise ConfigurationError('Select an autoescaping Jinja-compatible environment.')
        self.environment = environment
        super().__init__(self._render_template)

    def _render_template(self, name, context):
        environment = self.environment
        autoescape = environment.autoescape(name) if callable(environment.autoescape) else environment.autoescape
        if autoescape is not True:
            if inspect.iscoroutine(autoescape):
                autoescape.close()
            raise ConfigurationError('HTML template autoescaping must be enabled for the selected name.')
        template = environment.get_template(name)
        if getattr(template, 'environment', None) is not environment:
            raise ConfigurationError('The selected template must belong to the configured environment.')
        render = getattr(template, 'render_async' if getattr(environment, 'is_async', False) else 'render', None)
        if not callable(render):
            raise ConfigurationError('The selected template does not implement its environment render mode.')
        return render(**context)
