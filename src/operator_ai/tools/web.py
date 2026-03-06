from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from html.parser import HTMLParser
from urllib.parse import urlparse

import aiohttp
import trafilatura

from operator_ai.tools.registry import tool

logger = logging.getLogger("operator.tools.web")

DEFAULT_LIMIT = 16_000
MAX_LIMIT = 100_000
_MD_TIMEOUT = aiohttp.ClientTimeout(total=10)
_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=30)
_LLMS_TIMEOUT = aiohttp.ClientTimeout(total=5)

_session: aiohttp.ClientSession | None = None
_LLMS_CACHE_MAX = 128
_llms_txt_cache: OrderedDict[str, bool] = OrderedDict()


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None
    _llms_txt_cache.clear()


def _is_text(content_type: str | None) -> bool:
    if not content_type:
        return False
    return any(t in content_type for t in ("text/", "application/json", "application/xml"))


def _is_html(content_type: str | None) -> bool:
    return bool(content_type and "html" in content_type)


def _is_already_markdown(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".md") or path.endswith(".txt")


def _domain(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def _try_md_variant(session: aiohttp.ClientSession, url: str) -> str | None:
    """Try fetching the .md version of a URL per the llms.txt spec."""
    parsed = urlparse(url)
    md_path = parsed.path.rstrip("/") + ".md"
    md_url = parsed._replace(path=md_path).geturl()
    try:
        async with session.get(md_url, timeout=_MD_TIMEOUT, allow_redirects=True) as resp:
            if resp.status == 200 and _is_text(resp.content_type):
                body = await resp.text()
                if body.strip():
                    logger.info("Found .md variant: %s", md_url)
                    return body
    except (aiohttp.ClientError, TimeoutError):
        pass
    return None


async def _check_llms_txt(session: aiohttp.ClientSession, url: str) -> bool:
    """Check if the domain serves /llms.txt (cached per domain)."""
    domain = _domain(url)
    if domain in _llms_txt_cache:
        _llms_txt_cache.move_to_end(domain)
        return _llms_txt_cache[domain]
    try:
        check_url = f"{domain}/llms.txt"
        async with session.head(check_url, timeout=_LLMS_TIMEOUT, allow_redirects=True) as resp:
            exists = resp.status == 200
    except (aiohttp.ClientError, TimeoutError):
        exists = False
    if len(_llms_txt_cache) >= _LLMS_CACHE_MAX:
        _llms_txt_cache.popitem(last=False)
    _llms_txt_cache[domain] = exists
    if exists:
        logger.info("Domain %s has /llms.txt", domain)
    return exists


async def _extract_with_trafilatura(html: str) -> str | None:
    """Run trafilatura in a thread (sync + CPU-bound)."""
    return await asyncio.to_thread(trafilatura.extract, html, output_format="text")


class _TextExtractor(HTMLParser):
    _skip_tags = frozenset({"script", "style", "nav", "header", "footer", "aside", "noscript"})

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = False

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._skip_tags:
            self.skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip_tags:
            self.skip = False

    def handle_data(self, data: str) -> None:
        if not self.skip:
            text = data.strip()
            if text:
                self.parts.append(text)


def _fallback_extract(html: str) -> str:
    """Minimal fallback when trafilatura returns nothing (e.g. JS-only SPA)."""
    try:
        parser = _TextExtractor()
        parser.feed(html)
        return "\n".join(parser.parts)
    except Exception:
        return html[:MAX_LIMIT]


def _chunk(text: str, offset: int, limit: int) -> str:
    """Slice text by offset/limit and prepend a metadata header."""
    total = len(text)
    chunk = text[offset : offset + limit]
    lines: list[str] = []

    if total <= limit and offset == 0:
        lines.append(f"[web_fetch: {total} chars]")
    else:
        end = min(offset + limit, total)
        lines.append(f"[web_fetch: chars {offset}-{end} of {total}]")
        if end < total:
            lines.append(f"[use offset={end} for more]")

    lines.append("")
    lines.append(chunk)
    return "\n".join(lines)


@tool(description="Fetch URL content as clean text. Prefers LLM-friendly formats.")
async def web_fetch(url: str, offset: int = 0, limit: int = DEFAULT_LIMIT) -> str:
    """Fetch a URL and return extracted content.

    Args:
        url: The URL to fetch.
        offset: Character offset to start from for paginating large content.
        limit: Max characters to return per call (default 16000).
    """
    limit = min(limit, MAX_LIMIT)
    offset = max(offset, 0)

    try:
        session = await get_session()

        # For non-markdown URLs, try the .md variant first
        if not _is_already_markdown(url):
            md_content = await _try_md_variant(session, url)
            if md_content:
                has_llms = await _check_llms_txt(session, url)
                result = _chunk(md_content, offset, limit)
                if has_llms and offset == 0:
                    result += (
                        f"\n\n[This site has an LLM-friendly index at {_domain(url)}/llms.txt]"
                    )
                return result

        # Fetch the original URL
        async with session.get(url, timeout=_FETCH_TIMEOUT, allow_redirects=True) as resp:
            if resp.status != 200:
                return f"[HTTP {resp.status}]"

            content_type = resp.content_type

            if not _is_text(content_type):
                return f"[unsupported content type: {content_type}]"

            raw = await resp.text()

        # Determine extraction strategy
        if _is_html(content_type) and not _is_already_markdown(url):
            # HTML — extract clean content
            text = await _extract_with_trafilatura(raw)
            if not text:
                logger.info("trafilatura returned nothing, using fallback for %s", url)
                text = _fallback_extract(raw)
        else:
            # Already text/markdown/json — use as-is
            text = raw

        # Check for llms.txt (populates cache for this domain)
        has_llms = await _check_llms_txt(session, url)

        result = _chunk(text, offset, limit)

        if has_llms and offset == 0:
            domain = _domain(url)
            result += f"\n\n[This site has an LLM-friendly index at {domain}/llms.txt]"

        return result

    except Exception as e:
        return f"[error fetching URL: {e}]"
