from __future__ import annotations

import aiohttp


class ResponseTooLarge(ValueError):
    pass


async def read_limited(content: aiohttp.StreamReader, limit: int = 1_048_576) -> bytes:
    """Read a complete response, including fragmented/chunked HTTP responses."""
    buffer = bytearray()
    async for part in content.iter_chunked(65_536):
        if len(buffer) + len(part) > limit:
            raise ResponseTooLarge("Response body exceeds limit")
        buffer.extend(part)
    return bytes(buffer)
