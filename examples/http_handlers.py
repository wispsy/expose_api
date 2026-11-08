"""Explicit handler composition; credentials and persistence remain caller-owned."""

from expose_api import App, HTTPException, JSONResponse, Router


app = App()
api = Router(prefix='/api')


@app.route('/healthz', public=True, name='health')
def health(request):
    return JSONResponse({'ready': True})


@api.route('/search', public=True, name='search')
async def search(request):
    terms = request.query_params.getlist('q')
    if len(terms) != 1 or not 1 <= len(terms[0]) <= 512:
        raise HTTPException(400, 'Provide one search term of 1 to 512 characters.')
    return JSONResponse({'query': terms[0], 'results': [], 'health': request.url_for('health')})


app.include_router(api)


if __name__ == '__main__':
    app.run()
