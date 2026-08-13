"""Networking and URL handling for FeetBrowser.

This is the "from scratch" transport layer: raw sockets speaking HTTP/1.1,
TLS for https, plus support for data:, file: and view-source: URLs.
Nothing here wraps an existing HTTP client engine beyond Python's socket/ssl.
"""

import socket
import ssl
import gzip
import zlib
import base64
import os
import urllib.parse

# A tiny in-process cache keyed by URL string. Honors a very small subset of
# Cache-Control (max-age). Good enough to avoid re-fetching stylesheets.
import time

_CACHE = {}

DEFAULT_HEADERS = {
    "User-Agent": "FeetBrowser/0.1 (from-scratch; +https://example.invalid)",
    "Accept": "text/html,application/xhtml+xml,text/css,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

MAX_REDIRECTS = 10


class URL:
    """A parsed URL plus the logic to fetch it."""

    def __init__(self, url):
        self.raw = url
        self.view_source = False
        self.fragment = ""
        self.host = ""
        self.port = None
        self.path = ""

        if url.startswith("view-source:"):
            self.view_source = True
            url = url[len("view-source:"):]
            self.raw = url

        # Split scheme.
        if ":" not in url:
            # Bare host/path -> assume https.
            url = "https://" + url

        self.scheme, rest = url.split(":", 1)
        self.scheme = self.scheme.lower()

        if self.scheme in ("http", "https"):
            self._parse_http(rest)
        elif self.scheme == "file":
            # file:///path, file://host/path (host ignored) or file:/path
            if rest.startswith("//"):
                remainder = rest[2:]
                slash = remainder.find("/")
                self.path = remainder[slash:] if slash != -1 else "/"
            else:
                self.path = rest or "/"
        elif self.scheme == "data":
            self.data_payload = rest
        else:
            # Unknown scheme: parse it anyway so extensions can intercept it
            # through the toes handle hook. Fetching it still fails loudly.
            if rest.startswith("//"):
                self._parse_http(rest)
            else:
                self.host = ""
                self.path = rest or "/"
                self.port = None

    def _parse_http(self, rest):
        # rest looks like //host[:port]/path?query#frag
        if rest.startswith("//"):
            rest = rest[2:]
        # Strip fragment.
        if "#" in rest:
            rest, self.fragment = rest.split("#", 1)
        else:
            self.fragment = ""
        if "/" in rest:
            authority, path = rest.split("/", 1)
            self.path = "/" + path
        else:
            authority, self.path = rest, "/"
        if "@" in authority:
            authority = authority.split("@", 1)[1]  # drop userinfo
        if ":" in authority:
            self.host, port = authority.rsplit(":", 1)
            self.port = int(port)
        else:
            self.host = authority
            self.port = 443 if self.scheme == "https" else 80

    # -- URL resolution --------------------------------------------------

    def resolve(self, url):
        """Resolve a possibly-relative URL against this one."""
        if "://" in url or url.startswith("data:") or url.startswith("view-source:"):
            return URL(url)
        if url.startswith("//"):
            return URL(self.scheme + ":" + url)
        if url.startswith("#"):
            new = URL(str(self))
            new.fragment = url[1:]
            return new
        if not url.startswith("/"):
            # Relative to current directory.
            dir_path = self.path
            if not dir_path.endswith("/"):
                dir_path = dir_path.rsplit("/", 1)[0] + "/"
            url = dir_path + url
        # Normalize ../ and ./
        parts = []
        for seg in url.split("/"):
            if seg == "..":
                if parts:
                    parts.pop()
            elif seg == ".":
                continue
            else:
                parts.append(seg)
        norm = "/".join(parts)
        if not norm.startswith("/"):
            norm = "/" + norm
        port = "" if (self.port in (80, 443, None)) else f":{self.port}"
        return URL(f"{self.scheme}://{self.host}{port}{norm}")

    def __str__(self):
        prefix = "view-source:" if self.view_source else ""
        if self.scheme in ("http", "https"):
            port = "" if (self.port in (80, 443)) else f":{self.port}"
            frag = f"#{self.fragment}" if getattr(self, "fragment", "") else ""
            return f"{prefix}{self.scheme}://{self.host}{port}{self.path}{frag}"
        if self.scheme == "file":
            return f"{prefix}file://{self.path}"
        if self.scheme == "data":
            return f"{prefix}data:{self.data_payload}"
        return self.raw

    # -- Fetching --------------------------------------------------------

    def request(self, redirects_left=MAX_REDIRECTS, payload=None):
        """Return (headers_dict, body_str, content_type)."""
        if self.scheme == "file":
            return self._request_file()
        if self.scheme == "data":
            return self._request_data()
        return self._request_http(redirects_left, payload)

    def _request_file(self):
        try:
            with open(self.path, "rb") as f:
                raw = f.read()
        except (FileNotFoundError, IsADirectoryError, PermissionError) as e:
            if os.path.isdir(self.path):
                items = sorted(os.listdir(self.path))
                links = "".join(
                    f'<li><a href="file://{os.path.join(self.path, i)}">{i}</a></li>'
                    for i in items
                )
                body = f"<h1>Index of {self.path}</h1><ul>{links}</ul>"
                return {}, body, "text/html"
            return {}, f"<h1>Cannot open file</h1><p>{e}</p>", "text/html"
        ext = os.path.splitext(self.path)[1].lower()
        ctype = "text/html" if ext in (".html", ".htm") else "text/plain"
        return {}, raw.decode("utf8", "replace"), ctype

    def _request_data(self):
        # data:[<mediatype>][;base64],<data>
        meta, _, data = self.data_payload.partition(",")
        ctype = meta.split(";")[0] or "text/plain"
        if meta.endswith(";base64"):
            body = base64.b64decode(data).decode("utf8", "replace")
        else:
            body = urllib.parse.unquote(data)
        return {}, body, ctype

    def _request_http(self, redirects_left, payload):
        cache_key = str(self)
        if payload is None and cache_key in _CACHE:
            expires, entry = _CACHE[cache_key]
            if expires is None or expires > time.time():
                return entry
            else:
                del _CACHE[cache_key]

        # create_connection handles both IPv4 and IPv6 hosts.
        s = socket.create_connection((self.host, self.port), timeout=20)
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            s = ctx.wrap_socket(s, server_hostname=self.host)

        method = "POST" if payload is not None else "GET"
        headers = dict(DEFAULT_HEADERS)
        headers["Host"] = self.host
        headers["Connection"] = "close"
        body_bytes = b""
        if payload is not None:
            body_bytes = payload.encode("utf8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Content-Length"] = str(len(body_bytes))

        req = f"{method} {self.path} HTTP/1.1\r\n"
        for k, v in headers.items():
            req += f"{k}: {v}\r\n"
        req += "\r\n"
        try:
            s.sendall(req.encode("utf8") + body_bytes)
            raw = self._read_all(s)
        finally:
            s.close()

        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        status_line = lines[0].decode("latin1")
        # Status line is: HTTP/x.y <code> <explanation>
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise RuntimeError(f"Malformed status line: {status_line!r}")
        status = int(parts[1])

        resp_headers = {}
        for line in lines[1:]:
            if not line:
                continue
            k, _, v = line.decode("latin1").partition(":")
            resp_headers[k.strip().lower()] = v.strip()

        # Redirects.
        if status in (301, 302, 303, 307, 308) and "location" in resp_headers:
            if redirects_left <= 0:
                raise RuntimeError("Too many redirects")
            location = resp_headers["location"]
            new_url = self.resolve(location)
            follow_payload = payload if status in (307, 308) else None
            return new_url.request(redirects_left - 1, follow_payload)

        # Decode transfer-encoding and content-encoding.
        body = self._decode_body(body, resp_headers)
        charset = self._charset(resp_headers)
        text = body.decode(charset, "replace")
        content_type = resp_headers.get("content-type", "text/html").split(";")[0].strip()

        result = (resp_headers, text, content_type)

        # Cache if allowed.
        cc = resp_headers.get("cache-control", "")
        if payload is None and status == 200 and "no-store" not in cc:
            expires = None
            for part in cc.split(","):
                part = part.strip()
                if part.startswith("max-age="):
                    try:
                        expires = time.time() + int(part.split("=", 1)[1])
                    except ValueError:
                        expires = None
            if "max-age" in cc:
                _CACHE[cache_key] = (expires, result)

        return result

    @staticmethod
    def _read_all(s):
        chunks = []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _decode_body(body, headers):
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body = URL._dechunk(body)
        enc = headers.get("content-encoding", "").lower()
        if enc == "gzip":
            try:
                body = gzip.decompress(body)
            except (OSError, EOFError):
                pass
        elif enc == "deflate":
            try:
                body = zlib.decompress(body)
            except zlib.error:
                body = zlib.decompress(body, -zlib.MAX_WBITS)
        return body

    @staticmethod
    def _dechunk(body):
        out = b""
        while body:
            size_line, _, body = body.partition(b"\r\n")
            try:
                size = int(size_line.strip().split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                break
            out += body[:size]
            body = body[size + 2:]  # skip trailing CRLF
        return out

    @staticmethod
    def _charset(headers):
        ctype = headers.get("content-type", "")
        if "charset=" in ctype:
            return ctype.split("charset=", 1)[1].split(";")[0].strip()
        return "utf8"
