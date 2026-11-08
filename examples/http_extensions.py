"""Framework-only composition; the caller supplies assets and authentication."""

import argparse
import asyncio
import hashlib
import html
from contextlib import asynccontextmanager
from pathlib import Path

from expose_api import (App, BearerAuth, FileResponse, HTTPException, JSONResponse, MultipartLimits,
                        RouteDocs, StaticFiles, StreamingResponse, TemplateRenderer, UploadFile)


def create_app(directory, *, authenticate=None):
    root = Path(directory)
    @asynccontextmanager
    async def lifespan(app):
        app.state['running'] = True
        try:
            yield
        finally:
            app.state['running'] = False
    app = App(title='HTTP extension acceptance', lifespan=lifespan,
              authenticate=authenticate or BearerAuth(lambda token: None), auth_challenge='Bearer')
    app.mount('/assets', StaticFiles(root), name='assets')

    def render(name, context):
        return ('<!doctype html><title>HTTP extensions</title><h1>' + html.escape(context['title'])
                + '</h1><link rel="stylesheet" href="' + html.escape(context['url_for']('assets', path='site.css'), quote=True)
                + '"><form method="post" enctype="multipart/form-data" action="'
                + html.escape(context['url_for']('upload'), quote=True) + '"><input type="file" name="file"></form>')
    templates = TemplateRenderer(render)

    @app.route('/', public=True, name='page', docs=RouteDocs(summary='Render caller-owned HTML'))
    async def page(request):
        titles = request.query_params.getlist('title')
        if len(titles) > 1:
            raise HTTPException(400, 'Select one title.')
        return await templates.response(request, 'page.html', {'title': titles[0] if titles else 'HTTP extensions'})

    @app.route('/healthz', public=True, name='health')
    def health(request):
        return JSONResponse({'running': app.state.get('running', False)})

    @app.route('/download', public=True, name='download', docs=RouteDocs(responses={
        '200': {'description': 'Selected file'}, '206': {'description': 'Selected byte range'},
        '304': {'description': 'Unchanged'}, '416': {'description': 'Unsatisfiable range'}}))
    def download(request):
        return FileResponse(root / 'download.txt', filename='download.txt')

    @app.route('/stream', public=True, name='stream')
    def stream(request):
        async def chunks():
            try:
                yield b'first\n'
                await asyncio.sleep(0)
                yield b'second\n'
            finally:
                app.state['stream_closed'] = True
        return StreamingResponse(chunks(), media_type='text/plain', chunk_size=4)

    upload_docs = RouteDocs(
        request_body={
            'required': True,
            'content': {
                'multipart/form-data': {
                    'schema': {'type': 'object', 'required': ['file'],
                               'properties': {'file': {'type': 'string', 'format': 'binary'}}}
                }
            }
        },
        responses={'201': {'description': 'Bounded upload receipt'}},
    )
    @app.route('/upload', methods=('POST',), authenticate=True,
               authorize=lambda request: 'upload' in request.principal.scopes, name='upload', docs=upload_docs)
    async def upload(request):
        async with request.multipart(limits=MultipartLimits(max_total_bytes=1024 * 1024)) as form:
            files = form.getlist('file')
            if len(files) != 1 or not isinstance(files[0], UploadFile):
                raise HTTPException(422, 'Provide one file.')
            file = files[0]
            digest = hashlib.sha256()
            while chunk := await file.read(65536):
                digest.update(chunk)
            receipt = {'filename': file.filename, 'size': file.size, 'sha256': digest.hexdigest(),
                       'subject': request.principal.subject}
        app.state['upload_closed'] = file.closed
        return JSONResponse(receipt, status=201)

    @app.route('/openapi.json', public=True, docs=RouteDocs(include=False))
    def specification(request):
        return JSONResponse(app.openapi())
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('assets', type=Path)
    arguments = parser.parse_args()
    create_app(arguments.assets).run()
