"""Explicit HTTP delegation without implicit child lifecycle ownership."""

from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True, slots=True)
class MountedResponse:
    application: object
    scope: dict


def mounted_response(application, scope, path, root, prefix):
    child = dict(scope)
    child['root_path'] = root + prefix
    child['path'] = root + path
    if child['path'] != scope['path'] and 'raw_path' in scope:
        child['raw_path'] = quote(root, safe='/').encode('ascii') + scope['raw_path']
    if 'state' in child:
        child['state'] = child['state'].copy()
    return MountedResponse(application, child)
