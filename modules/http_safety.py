"""Bounded public HTTP reads for user-configured social feeds."""
from contextlib import asynccontextmanager
import time
import ipaddress
import json
import socket

import aiohttp
from yarl import URL

MAX_BODY_BYTES = 2 * 1024 * 1024


def require_public_address(value: str) -> None:
    address = ipaddress.ip_address(value.split('%', 1)[0])
    mapped = getattr(address, 'ipv4_mapped', None)
    if mapped is not None:
        address = mapped
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise ValueError('Social source must resolve to a public Internet address')


def validate_public_url(value: str) -> URL:
    url = URL(value)
    if url.scheme not in {'http', 'https'} or not url.host or url.user is not None or url.password is not None:
        raise ValueError('Social source must be an HTTP(S) URL without credentials')
    try:
        ipaddress.ip_address(url.host.split('%', 1)[0])
    except ValueError:
        if '%' in url.host or url.host.lower().rstrip('.') == 'localhost':
            raise ValueError('Local social sources are not supported')
    else:
        require_public_address(url.host)
    return url


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Check the actual records handed to the connector, not a separate DNS probe."""
    def __init__(self):
        self._resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await self._resolver.resolve(host, port, family)
        if not records:
            raise ValueError('Social source has no public address')
        for record in records:
            require_public_address(record['host'])
        return records

    async def close(self):
        await self._resolver.close()


class BoundedResponse:
    def __init__(self, response):
        self._response = response
        self.status = response.status

    async def text(self):
        chunks = []
        size = 0
        async for chunk in self._response.content.iter_chunked(65536):
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise ValueError('Social response exceeds 2 MiB')
            chunks.append(chunk)
        encoding = self._response.charset or 'utf-8'
        return b''.join(chunks).decode(encoding, errors='replace')

    async def json(self, **_kwargs):
        return json.loads(await self.text())


@asynccontextmanager
async def safe_public_get(session, url, *, timeout=None, ssl=True, headers=None):
    """Pin public DNS resolution and validate every redirect before connecting."""
    current = validate_public_url(str(url))
    request_timeout = timeout or aiohttp.ClientTimeout(total=20)
    total = request_timeout.total or 20
    resolver = PublicResolver()
    request_headers = dict(session.headers)
    request_headers.update(headers or {})
    deadline = time.monotonic() + total
    try:
        async with aiohttp.ClientSession(
            headers=request_headers,
            timeout=request_timeout,
            trust_env=False,
            connector=aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False),
        ) as public_session:
            for _ in range(6):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Social request timed out')
                async with public_session.get(current, ssl=ssl, allow_redirects=False,
                                              timeout=aiohttp.ClientTimeout(total=remaining)) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.headers.get('Location')
                        if not location:
                            raise ValueError('Social redirect has no destination')
                        current = validate_public_url(str(current.join(URL(location))))
                        continue
                    yield BoundedResponse(response)
                    return
            raise ValueError('Too many social redirects')
    finally:
        await resolver.close()
