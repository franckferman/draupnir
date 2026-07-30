"""Minimal hardened HTTP client built on urllib.

No third-party dependency on purpose: a mirroring tool that must still run in
five years should not rot because a TLS/HTTP stack changed its API. What we
lose from `requests` is re-implemented here, narrowly:

  * bounded retries with exponential backoff + jitter
  * Retry-After awareness (seconds *and* HTTP-date forms)
  * gzip/deflate transport decoding
  * cross-host redirect refusal (a forge redirect must not exfiltrate a token)
  * response size ceiling so a hostile endpoint cannot exhaust memory
"""

from __future__ import annotations

import email.utils
import gzip
import json
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import HttpError, RateLimited

__all__ = ["DEFAULT_USER_AGENT", "HttpClient", "Response"]

DEFAULT_USER_AGENT = "draupnir (+https://github.com/franckferman/draupnir)"

_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_MAX_BODY = 64 * 1024 * 1024  # 64 MiB ceiling on a single API response


@dataclass(frozen=True)
class Response:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def text(self, encoding: str = "") -> str:
        if not encoding:
            ctype = self.header("content-type")
            encoding = "utf-8"
            if "charset=" in ctype:
                encoding = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        return self.body.decode(encoding, errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text())
        except json.JSONDecodeError as exc:
            preview = self.text()[:120].replace("\n", " ")
            raise HttpError(
                f"response is not valid JSON ({exc.msg}); body starts with: {preview!r}",
                url=self.url,
                status=self.status,
            ) from exc


class _StrictRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects only while the host stays the same.

    Prevents a compromised/misconfigured forge from bouncing an Authorization
    header to a third party, and stops http->somewhere-else downgrades.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        old_host = urllib.parse.urlsplit(req.full_url).netloc.lower()
        new = urllib.parse.urlsplit(newurl)
        if new.scheme not in {"http", "https"}:
            raise HttpError(f"refusing redirect to non-HTTP scheme: {newurl}", url=req.full_url)
        if new.netloc.lower() != old_host:
            raise HttpError(
                f"refusing cross-host redirect: {old_host} -> {new.netloc}", url=req.full_url
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class HttpClient:
    """Small synchronous client. Thread-safe: urlopen holds no shared state."""

    token: str = ""
    timeout: float = 30.0
    retries: int = 4
    backoff: float = 0.75
    backoff_cap: float = 30.0
    user_agent: str = DEFAULT_USER_AGENT
    verify_tls: bool = True
    max_retry_after: float = 120.0
    sleeper: Callable[[float], None] = time.sleep
    _opener: urllib.request.OpenerDirector = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.verify_tls:
            context = ssl.create_default_context()
        else:
            context = ssl._create_unverified_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            _StrictRedirectHandler(),
        )
        # Never let urllib pick up proxy/auth state we did not ask for.
        self._opener.addheaders = []

    # -- public ----------------------------------------------------------

    def get(self, url: str, params: Mapping[str, Any] | None = None) -> Response:
        return self.request("GET", url, params=params)

    def get_json(self, url: str, params: Mapping[str, Any] | None = None) -> tuple[Any, Response]:
        response = self.get(url, params)
        return response.json(), response

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept: str = "application/json",
    ) -> Response:
        """Perform a request, retrying transient failures.

        Raises HttpError (or RateLimited) once the retry budget is spent.
        """
        full_url = _with_params(url, params)
        _guard_scheme(full_url)
        attempts = max(1, self.retries + 1)
        last: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self._once(method, full_url, accept)
            except HttpError as exc:
                last = exc
                retryable = exc.status in _RETRY_STATUSES or exc.status == 0
                if not retryable or attempt == attempts:
                    raise
                self.sleeper(self._delay(attempt, getattr(exc, "retry_after", 0.0)))
            except (urllib.error.URLError, OSError) as exc:  # transport-level
                last = HttpError(f"{type(exc).__name__}: {exc}", url=full_url)
                if attempt == attempts:
                    raise last from exc
                self.sleeper(self._delay(attempt, 0.0))

        raise last or HttpError("request failed", url=full_url)  # pragma: no cover

    # -- internals -------------------------------------------------------

    def _once(self, method: str, url: str, accept: str) -> Response:
        request = urllib.request.Request(url, method=method)
        request.add_header("User-Agent", self.user_agent)
        request.add_header("Accept", accept)
        request.add_header("Accept-Encoding", "gzip, deflate")
        if self.token:
            # Gitea/Forgejo accept both; "token" is the documented PAT form.
            request.add_header("Authorization", f"token {self.token}")

        try:
            with self._opener.open(request, timeout=self.timeout) as raw:
                body = _decode(raw.read(_MAX_BODY + 1), raw.headers.get("Content-Encoding", ""))
                if len(body) > _MAX_BODY:
                    raise HttpError(f"response exceeds {_MAX_BODY} bytes", url=url)
                headers = {k.lower(): v for k, v in raw.headers.items()}
                return Response(url=url, status=raw.status, headers=headers, body=body)
        except urllib.error.HTTPError as exc:
            detail = _short_body(exc)
            retry_after = _retry_after(exc.headers, self.max_retry_after)
            message = f"HTTP {exc.code} from {url}"
            if detail:
                message = f"{message}: {detail}"
            error: HttpError
            if exc.code == 429:
                error = RateLimited(message, url=url, status=exc.code)
            else:
                error = HttpError(message, url=url, status=exc.code)
            error.retry_after = retry_after  # type: ignore[attr-defined]
            raise error from exc

    def _delay(self, attempt: int, retry_after: float) -> float:
        if retry_after > 0:
            return min(retry_after, self.max_retry_after)
        window = min(self.backoff * (2 ** (attempt - 1)), self.backoff_cap)
        return window * (0.5 + random.random() / 2)


def _guard_scheme(url: str) -> None:
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in {"http", "https"}:
        raise HttpError(f"unsupported URL scheme {scheme!r}", url=url)


def _with_params(url: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return url
    clean = {k: _param(v) for k, v in params.items() if v is not None}
    if not clean:
        return url
    split = urllib.parse.urlsplit(url)
    merged = urllib.parse.parse_qsl(split.query, keep_blank_values=True)
    merged.extend(clean.items())
    return urllib.parse.urlunsplit(split._replace(query=urllib.parse.urlencode(merged)))


def _param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _decode(raw: bytes, encoding: str) -> bytes:
    encoding = encoding.lower().strip()
    try:
        if encoding == "gzip":
            return gzip.decompress(raw)
        if encoding == "deflate":
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error):
        return raw  # server lied about the encoding; use bytes as-is
    return raw


def _short_body(exc: urllib.error.HTTPError) -> str:
    try:
        payload = exc.read(2048)
    except Exception:  # pragma: no cover - body already consumed
        return ""
    text = payload.decode("utf-8", errors="replace").strip()
    if text.startswith("{"):
        try:
            blob = json.loads(text)
            if isinstance(blob, dict):
                text = str(blob.get("message") or blob.get("errors") or text)
        except json.JSONDecodeError:
            pass
    single = " ".join(text.split())
    return single[:200]


def _retry_after(headers: Any, ceiling: float) -> float:
    raw = ""
    try:
        raw = (headers.get("Retry-After") or "").strip()
    except AttributeError:  # pragma: no cover
        return 0.0
    if not raw:
        return 0.0
    try:
        return max(0.0, min(float(raw), ceiling))
    except ValueError:
        pass
    try:
        stamp = email.utils.parsedate_to_datetime(raw)
    except (ValueError, TypeError):  # not a number and not an HTTP-date
        return 0.0
    if stamp is None:  # pragma: no cover - older stdlib returned None
        return 0.0
    delta = stamp.timestamp() - time.time()
    return max(0.0, min(delta, ceiling))
