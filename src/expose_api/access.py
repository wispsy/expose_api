"""Caller-owned immutable identity and synchronous, detached record policies."""

import inspect
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass

from .errors import AccessDeniedError, ConfigurationError, NotFoundError


def _identifier(value):
    return type(value) is str and 1 <= len(value) <= 256 and all(ord(c) >= 32 and ord(c) != 127 for c in value)


@dataclass(frozen=True, slots=True)
class Principal:
    subject: str
    tenant: str | None = None
    scopes: frozenset[str] = frozenset()

    def __post_init__(self):
        if (not _identifier(self.subject) or self.tenant is not None and not _identifier(self.tenant)
                or type(self.scopes) is not frozenset or len(self.scopes) > 64
                or not all(_identifier(scope) for scope in self.scopes)):
            raise ConfigurationError("Principal identifiers and scopes must be bounded immutable strings.")


class BearerAuth:
    """Parse one bounded Authorization header; the caller verifies its token."""

    def __init__(self, verify):
        if not callable(verify):
            raise ConfigurationError("Bearer authentication requires a token verifier.")
        self.verify = verify

    async def __call__(self, request):
        header = request.header("authorization")
        if header is None or len(header) > 8192:
            return None
        match = re.fullmatch(r"(?i:Bearer) ([A-Za-z0-9._~+/-]+=*)", header)
        if match is None:
            return None
        result = self.verify(match[1])
        return await result if inspect.isawaitable(result) else result


@dataclass(frozen=True, slots=True)
class Authenticated:
    """Require verified identity, all scopes, optional tenant and every check."""

    scopes: frozenset[str] = frozenset()
    tenant: bool | str = False
    checks: tuple = ()

    def __post_init__(self):
        if isinstance(self.scopes, str) or isinstance(self.checks, str):
            raise ConfigurationError('Select collections of scopes and permission checks.')
        try:
            scopes, checks = frozenset(self.scopes), tuple(self.checks)
        except TypeError:
            raise ConfigurationError('Select collections of scopes and permission checks.') from None
        if (len(scopes) > 64 or not all(_identifier(scope) for scope in scopes)
                or type(self.tenant) is not bool and not _identifier(self.tenant)
                or any(not callable(check) for check in checks)):
            raise ConfigurationError('Select bounded scopes, a tenant requirement and callable checks.')
        object.__setattr__(self, 'scopes', scopes)
        object.__setattr__(self, 'checks', checks)

    async def __call__(self, request):
        principal = request.principal
        if (type(principal) is not Principal or not self.scopes <= principal.scopes
                or self.tenant is True and principal.tenant is None
                or type(self.tenant) is str and principal.tenant != self.tenant):
            return False
        for check in self.checks:
            allowed = check(request)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if type(allowed) is not bool:
                raise ConfigurationError('Policy checks must return a boolean.')
            if not allowed:
                return False
        return True


@dataclass(frozen=True, slots=True)
class RecordAccess:
    principal: Principal
    policy: Callable
    scan_limit: int = 10000

    def __post_init__(self):
        if type(self.principal) is not Principal or not callable(self.policy) or inspect.iscoroutinefunction(self.policy):
            raise ConfigurationError("Record access requires a Principal and synchronous policy.")
        if type(self.scan_limit) is not int or not 1 <= self.scan_limit <= 10000:
            raise ConfigurationError("scan_limit must be between 1 and 10000.")

    def allows(self, operation, value):
        result = self.policy(self.principal, operation, deepcopy(value))
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise ConfigurationError("Record policies must return a boolean synchronously.")
        if type(result) is not bool:
            raise ConfigurationError("Record policies must return a boolean.")
        return result

    def check(self, operation, value, *, conceal=False):
        if not self.allows(operation, value):
            if conceal:
                raise NotFoundError("Record not found.")
            raise AccessDeniedError("Operation not permitted.")
