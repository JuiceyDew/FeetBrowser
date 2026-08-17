"""Networking and URL handling for FeetBrowser.

This is the "from scratch" transport layer: raw sockets speaking HTTP/1.1,
TLS for https, plus support for data:, file: and view-source: URLs.
Nothing here wraps an existing HTTP client engine beyond Python's socket/ssl.
"""

import base64
import codecs
import os
import socket
import ssl
import threading
import time
import urllib.parse
import zlib

from feetbrowser.h2 import H2Connection, H2Error

# A tiny in-process cache keyed by URL string. Honors a very small subset of
# Cache-Control (max-age). Good enough to avoid re-fetching stylesheets.
_CACHE = {}
CACHE_MAX_SIZE = 1000

# Bounded cache of resolved host addresses. Image-heavy pages fetch dozens of
# resources from the same host; without this, every request pays a fresh
# getaddrinfo (a blocking, sometimes multi-second DNS round-trip). Entries are
# (timestamp, (family, socktype, proto, sockaddr)) tuples; a failed connect
# drops the entry and falls back to the full resolver. A lock guards access
# because image fetches run on background threads, and a TTL keeps rotated DNS
# from pinning the browser to a stale endpoint forever.
_DNS_CACHE = {}
_DNS_CACHE_MAX = 512
_DNS_TTL = 300.0  # seconds
_DNS_LOCK = threading.Lock()

# How long a name lookup may block before we give up on it. getaddrinfo is a
# blocking call into the platform resolver: socket.settimeout does not apply
# to it, and there is no portable way to interrupt one in flight -- the usual
# escape hatch, SIGALRM, does not exist on Windows at all. A resolver that
# never answers (a VPN, a captive portal, an unreachable corporate DNS
# server) would otherwise stop the browser dead with nothing printed and no
# way out but killing it. Resolving on a worker thread and giving up on the
# join puts a ceiling on it, at the cost of leaving that thread behind: it is
# a daemon, so a wedged lookup cannot keep the process alive.
_DNS_TIMEOUT = 20.0  # seconds

# Lookups currently in flight, keyed like _DNS_CACHE. Callers that want a host
# somebody else is already resolving wait on that one worker instead of
# starting another, so a slow resolver costs one thread per host rather than
# one per request -- a page with thirty images on a stalled origin would
# otherwise start thirty identical lookups.
_DNS_INFLIGHT = {}

# Bounded pool of idle keep-alive connections, keyed by (scheme, host, port).
# HTTP/1.1 lets one connection serve several requests to the same origin, which
# skips the fresh TCP + TLS handshake each resource used to pay, the most
# expensive part of a fetch. Sockets are parked after a fully-framed response
# and reclaimed by the next request to that origin; a parked socket whose peer
# already closed it is detected and retried once on a fresh connection. A lock
# guards access because image fetches run on background threads, and the TTL
# bounds how long we hold a socket that a page may not use again.
_CONN_POOL = {}
_CONN_POOL_MAX_PER_ORIGIN = 4
_CONN_POOL_MAX_TOTAL = 64
_CONN_POOL_TTL = 30.0  # seconds idle before a parked socket is closed
_CONN_LOCK = threading.Lock()


def _close_socket(s):
    # None is allowed: the cleanup paths in _request_http run from `except`
    # blocks that can be reached before a socket was ever opened -- a failed
    # lookup or a refused connect leaves `s` unset. Closing it there used to
    # raise AttributeError, which is not an OSError and so escaped the guard
    # below, replacing the real network error with a confusing one.
    if s is None:
        return
    try:
        s.close()
    except OSError:
        pass


def _pool_take(key):
    """Pop the most recently parked idle socket for `key`, or None. Stale
    (TTL-expired) parked sockets are closed rather than reused."""
    with _CONN_LOCK:
        lst = _CONN_POOL.get(key)
        if not lst:
            _CONN_POOL.pop(key, None)
            return None
        now = time.time()
        while lst:
            s, parked = lst.pop()
            if now - parked > _CONN_POOL_TTL:
                _close_socket(s)
                continue
            if not lst:
                del _CONN_POOL[key]
            return s
        _CONN_POOL.pop(key, None)
        return None


def _pool_park(key, s):
    """Return a healthy socket to the idle pool, evicting the oldest socket
    for the same origin (and a random origin) when the pool is full."""
    with _CONN_LOCK:
        if len(_CONN_POOL) >= _CONN_POOL_MAX_TOTAL and key not in _CONN_POOL:
            _close_socket(s)
            return
        lst = _CONN_POOL.setdefault(key, [])
        if len(lst) >= _CONN_POOL_MAX_PER_ORIGIN:
            _close_socket(lst.pop(0)[0])
        lst.append((s, time.time()))


# Multiplexed HTTP/2 connections, keyed by origin. Unlike the HTTP/1.1 pool,
# a connection is not taken out of service while a request is in flight: many
# streams share it, so several threads can hold the same H2Connection and make
# concurrent requests at once. The lock only guards the dict, and a dead
# connection (peer GOAWAY, socket failure) is dropped so the next request to
# that origin starts fresh.
_H2_POOL = {}
_H2_LOCK = threading.Lock()


def _h2_take(origin):
    with _H2_LOCK:
        conn = _H2_POOL.get(origin)
        if conn is None:
            return None
        if conn.dead:
            del _H2_POOL[origin]
            return None
        return conn


def _h2_park(origin, conn):
    with _H2_LOCK:
        existing = _H2_POOL.get(origin)
        if existing is not None and existing is not conn and not existing.dead:
            # Another thread won the race to open a fresh connection; ours is
            # redundant, so close it and keep using the live one.
            conn.close()
            return
        _H2_POOL[origin] = conn


def _h2_drop(origin, conn):
    with _H2_LOCK:
        if _H2_POOL.get(origin) is conn:
            del _H2_POOL[origin]
    conn.close()


def _alpn_proto(s):
    """The protocol a TLS socket negotiated, or None (plain TCP, or a Python
    build without ALPN support)."""
    try:
        return s.selected_alpn_protocol()
    except (AttributeError, OSError):
        return None


def _resolve(host, port, timeout=None):
    """socket.getaddrinfo with a ceiling on how long it may block.

    Raises socket.timeout if the resolver has not answered within `timeout`
    seconds. The lookup itself cannot be cancelled, so the worker is left to
    finish (or not) on its own; what is bounded is how long *we* wait.
    """
    if timeout is None:
        timeout = _DNS_TIMEOUT
    key = (host, port)
    with _DNS_LOCK:
        cell = _DNS_INFLIGHT.get(key)
        mine = cell is None
        if mine:
            cell = {"done": threading.Event()}
            _DNS_INFLIGHT[key] = cell

    if mine:
        def work():
            try:
                cell["infos"] = socket.getaddrinfo(
                    host, port, 0, socket.SOCK_STREAM)
            except BaseException as exc:  # re-raised on the waiting thread
                cell["error"] = exc
            finally:
                with _DNS_LOCK:
                    if _DNS_INFLIGHT.get(key) is cell:
                        del _DNS_INFLIGHT[key]
                cell["done"].set()

        threading.Thread(target=work, name=f"dns-{host}", daemon=True).start()

    if not cell["done"].wait(timeout):
        raise socket.timeout(
            f"DNS lookup for {host} took longer than {timeout:g}s")
    if "error" in cell:
        raise cell["error"]
    infos = cell.get("infos")
    if not infos:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    return infos


def _connect_any(host, port):
    """What socket.create_connection does -- try every address the host
    reports, keep the first that answers -- with the name lookup bounded the
    same way the cached path's is. create_connection resolves internally, so
    calling it here would reintroduce the unbounded getaddrinfo this avoids.
    """
    last = None
    for family, socktype, proto, _canon, sockaddr in _resolve(host, port):
        s = None
        try:
            s = socket.socket(family, socktype, proto)
            s.settimeout(20)
            s.connect(sockaddr)
            return s
        except OSError as exc:
            if s is not None:
                _close_socket(s)
            last = exc
    if last is not None:
        raise last
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


def _connect(host, port):
    """Open a TCP socket to (host, port), reusing a cached address when one
    is available and falling back to the full resolver (which retries across
    all resolved addresses) otherwise."""
    key = (host, port)
    now = time.time()
    with _DNS_LOCK:
        entry = _DNS_CACHE.get(key)
        if entry is not None and now - entry[0] > _DNS_TTL:
            _DNS_CACHE.pop(key, None)
            entry = None
    if entry is None:
        # Deliberately outside the lock. The lock is here to keep concurrent
        # image fetches from corrupting the dict, not to serialise them
        # against the network: holding it across a lookup would make one slow
        # resolver stall every other thread that wanted any host at all.
        infos = _resolve(host, port)
        info = infos[0][:3] + (infos[0][4],)
        entry = (now, info)
        with _DNS_LOCK:
            if len(_DNS_CACHE) < _DNS_CACHE_MAX:
                _DNS_CACHE[key] = entry
    info = entry[1]
    s = None
    try:
        s = socket.socket(info[0], info[1], info[2])
        s.settimeout(20)
        s.connect(info[3])
        return s
    except OSError:
        # Cached address unreachable (rotated DNS / load balancer): drop it
        # and let the full resolver try every address the host reports.
        if s is not None:
            s.close()
        with _DNS_LOCK:
            if _DNS_CACHE.get(key) == entry:
                _DNS_CACHE.pop(key, None)
        s = _connect_any(host, port)
        # Cache the address the fallback actually connected to, so a repeat
        # request skips the failed attempt.
        try:
            peer = s.getpeername()
            with _DNS_LOCK:
                if len(_DNS_CACHE) < _DNS_CACHE_MAX:
                    _DNS_CACHE[key] = (time.time(),
                                       (s.family, s.type, s.proto, peer))
        except OSError:
            pass
        return s

DEFAULT_HEADERS = {
    "User-Agent": "FeetBrowser/0.1.1 (https://github.com/JuiceyDew/FeetBrowser)",
    "Accept": "text/html,application/xhtml+xml,text/css,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

MAX_REDIRECTS = 10

#: Hard cap on any single response (headers + body) read from the network or
#: from a local file, and on the decompressed size of a gzip/deflate payload.
#: Keeps a hostile peer from exhausting memory with an unbounded stream or a
#: decompression bomb.
_MAX_BODY_BYTES = 64 * 1024 * 1024

# Schemes the URL parser understands (used to distinguish a bare host from a
# scheme-less string like "example.com:8080").
KNOWN_SCHEMES = {"http", "https", "file", "data"}


def _is_drive(text):
    """True for "C:" or the older "C|", a Windows drive in a file: URL."""
    return len(text) == 2 and text[0].isalpha() and text[1] in ":|"


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

        # Split scheme. Anything without a known scheme is treated as a bare
        # host/path (with optional port) and assumed to be https.
        if "://" not in url:
            head = url.split(":", 1)[0].lower()
            if head not in KNOWN_SCHEMES:
                url = "https://" + url

        self.scheme, rest = url.split(":", 1)
        self.scheme = self.scheme.lower()

        if self.scheme in ("http", "https"):
            self._parse_http(rest)
        elif self.scheme == "file":
            self._parse_file(rest)
        elif self.scheme == "data":
            self.data_payload = rest
        else:
            # Unknown scheme: parse it anyway so extensions can intercept it
            # through the toes handle hook. Fetching it still fails loudly.
            if rest.startswith("//"):
                if len(rest) > 2:
                    self._parse_http(rest)
                else:
                    # Empty host (e.g. toehub://): keep host empty so an
                    # extension scheme can still route on it.
                    self.host = ""
                    self.path = "/"
                    self.port = None
            else:
                self.host = ""
                self.path = rest or "/"
                self.port = None

    def _parse_file(self, rest):
        """Parse the path out of a file: URL, in any of the shapes people
        actually type.

        The plain ones are ``file:///path``, ``file://host/path`` (the host is
        ignored) and ``file:/path``. Windows adds two more, because a drive
        letter does not fit the grammar: ``file://C:/x`` puts the drive where
        the host goes, and a path pasted out of Explorer arrives with
        backslashes.

        What comes out is still a *URL* path -- forward slashes, leading
        slash, percent-escapes intact -- so resolve(), str() and the
        same-origin check all keep working in one coordinate system.
        ``local_path()`` is what turns it back into something open() takes.
        """
        path = rest.replace("\\", "/")
        if path.startswith("//"):
            remainder = path[2:]
            slash = remainder.find("/")
            authority = remainder if slash == -1 else remainder[:slash]
            if _is_drive(authority):
                path = "/" + remainder
            elif slash != -1:
                path = remainder[slash:]
            else:
                path = "/"
        self.path = path if path.startswith("/") else "/" + path

    def local_path(self):
        """The filesystem path this file: URL names.

        A URL path and a filesystem path are different strings, and on Windows
        they are not even the same shape: ``file:///C:/x`` carries a leading
        slash that ``open()`` must not see, and the separator is a backslash.
        Percent-escapes come off here too, which is what makes a directory
        listing's own links openable -- it writes them escaped.
        """
        path = urllib.parse.unquote(self.path)
        if _is_drive(path[1:3]):
            path = path[1] + ":" + path[3:] if len(path) > 3 else path[1] + ":"
        return path if os.sep == "/" else path.replace("/", os.sep)

    def _parse_http(self, rest):
        # rest looks like //host[:port]/path?query#frag
        if rest.startswith("//"):
            rest = rest[2:]
        # Strip fragment (self.fragment is already "" by default).
        if "#" in rest:
            rest, self.fragment = rest.split("#", 1)
        if "/" in rest:
            authority, path = rest.split("/", 1)
            self.path = "/" + path
        else:
            authority, self.path = rest, "/"
        if "@" in authority:
            authority = authority.rsplit("@", 1)[1]  # drop userinfo
        # IPv6 literal (optionally with port): [::1]:8080
        if authority.startswith("["):
            if "]" in authority:
                host, _, port_part = authority.partition("]")
                self.host = host[1:]
                port = port_part[1:] if port_part.startswith(":") else None
            else:
                raise ValueError(f"Malformed IPv6 address in URL: {authority!r}")
        elif ":" in authority:
            self.host, port = authority.rsplit(":", 1)
        else:
            self.host, port = authority, None

        if not self.host:
            raise ValueError(f"Missing host in URL: {authority!r}")
        self.host = self.host.lower()
        if port is None or port == "":
            self.port = 443 if self.scheme == "https" else 80
        else:
            try:
                self.port = int(port)
            except ValueError:
                raise ValueError(f"Invalid port in URL: {port!r}")
        if not (0 < self.port <= 65535):
            raise ValueError(f"Port out of range in URL: {self.port}")

    # -- URL resolution --------------------------------------------------

    def resolve(self, url):
        """Resolve a possibly-relative URL against this one."""
        if "://" in url or url.startswith(("data:", "view-source:")):
            return URL(url)
        if url.startswith("//"):
            return URL(self.scheme + ":" + url)
        if url.startswith("#"):
            new = URL(str(self))
            new.fragment = url[1:]
            return new
        if not url.startswith("/"):
            # Relative to current directory.
            dir_path = self.path.rpartition("/")[0] + "/"
            url = dir_path + url
        # Normalize ../ and ./
        parts = []
        for seg in url.split("/"):
            if seg == ".." and parts:
                parts.pop()
            elif seg not in ("", ".", ".."):
                parts.append(seg)
        new_url = URL(f"{self.scheme}://{self.netloc()}{'/' + '/'.join(parts)}")
        new_url.view_source = self.view_source
        return new_url

    def netloc(self):
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = "" if (self.port in (80, 443, None)) else f":{self.port}"
        return f"{host}{port}"

    def _adopt(self, other):
        """Make `self` look like `other`. Used to follow HTTP redirects in
        place so a URL object reports the location it actually fetched
        content from. Callers resolve relative URLs against the page URL and
        must not keep the pre-redirect host, or every relative resource
        (images, stylesheets, scripts) is fetched from the wrong server."""
        self.scheme = other.scheme
        self.host = other.host
        self.port = other.port
        self.path = other.path
        self.fragment = other.fragment
        self.view_source = other.view_source
        self.raw = other.raw
        if hasattr(other, "data_payload"):
            self.data_payload = other.data_payload

    def __str__(self):
        prefix = "view-source:" if self.view_source else ""
        if self.scheme in ("http", "https"):
            frag = f"#{self.fragment}" if getattr(self, "fragment", "") else ""
            return f"{prefix}{self.scheme}://{self.netloc()}{self.path}{frag}"
        if self.scheme == "file":
            return f"{prefix}file://{self.path}"
        if self.scheme == "data":
            return f"{prefix}data:{self.data_payload}"
        return self.raw

    # -- Fetching --------------------------------------------------------

    def request(self, redirects_left=MAX_REDIRECTS, payload=None, raw=False,
                refresh=False):
        """Return (headers_dict, body, content_type).

        `raw=True` skips text decoding and returns the body as bytes (used by
        image fetches). The flag is threaded through redirects so an image
        served from a redirect location still comes back undecoded.
        `refresh=True` bypasses the response cache so callers (e.g. the
        reload button) always get a fresh copy.
        """
        if self.scheme == "file":
            return self._request_file(raw)
        if self.scheme == "data":
            return self._request_data(raw)
        return self._request_http(redirects_left, payload, raw, refresh)

    def request_bytes(self, redirects_left=MAX_REDIRECTS):
        """Return (headers_dict, body_bytes, content_type) for binary data
        (images), skipping the text decoding that request() applies."""
        return self.request(redirects_left, raw=True)

    def request_impersonated(self):
        """Fetch via curl_cffi impersonating a Chrome browser.

        Our raw socket/ssl stack sends a ClientHello that sites like Google
        fingerprint as a bot, so they serve an "enable JavaScript" stub
        instead of their real (JS-driven) application. curl_cffi is built
        against BoringSSL and reproduces Chrome's TLS + HTTP/2 + header
        fingerprints, which residential clients on such sites are served the
        full app. Returns (headers_dict, body_str, content_type).

        Requires the optional `curl_cffi` package; falls back to
        `request()` if it isn't installed.
        """
        try:
            from curl_cffi import requests as cffi
        except ImportError:
            return self.request()
        headers = dict(DEFAULT_HEADERS)
        r = cffi.get(str(self), impersonate="chrome", headers=headers,
                     timeout=30, allow_redirects=True)
        ctype = r.headers.get("content-type", "text/html").split(";")[0].strip()
        return dict(r.headers), r.text, ctype

    def request_impersonated_bytes(self, redirects_left=MAX_REDIRECTS):
        """The impersonated fetch, with the body as bytes for images.

        Pages already ride the impersonated path; images used to go over the
        raw socket stack instead, and sites whose bot management fingerprints
        every connection (safebooru behind Cloudflare) throttle that client
        into hanging bursts, so a page full of thumbnails drew placeholders.
        Presenting the same Chrome fingerprint the page fetch did keeps the
        ``<img>`` requests on the same side of the gate.
        """
        try:
            from curl_cffi import requests as cffi
        except ImportError:
            return self.request_bytes(redirects_left)
        headers = dict(DEFAULT_HEADERS)
        r = cffi.get(str(self), impersonate="chrome", headers=headers,
                     timeout=30, allow_redirects=True)
        ctype = r.headers.get("content-type", "text/html").split(";")[0].strip()
        return dict(r.headers), r.content, ctype

    def _request_file(self, raw=False):
        local = self.local_path()
        try:
            with open(local, "rb") as f:
                payload = f.read(_MAX_BODY_BYTES + 1)
        # OSError, not the three obvious subclasses: a Windows path can be
        # rejected outright (a bad drive, a reserved name), and that arrives
        # as a plain OSError or ValueError rather than FileNotFoundError.
        except (OSError, ValueError) as e:
            if os.path.isdir(local):
                # The links are built in URL space, not with os.path.join --
                # a backslash in an href is not a path separator, it is a
                # character that has to be escaped.
                base = self.path if self.path.endswith("/") else self.path + "/"
                links = "".join(
                    f'<li><a href="file://{urllib.parse.quote(base + i)}">{i}</a></li>'
                    for i in sorted(os.listdir(local)))
                body = f"<h1>Index of {local}</h1><ul>{links}</ul>"
            else:
                body = f"<h1>Cannot open file</h1><p>{e}</p>"
            return {}, body.encode("utf8", "replace") if raw else body, \
                "text/html"
        if len(payload) > _MAX_BODY_BYTES:
            body = f"<h1>File too large to open</h1><p>{local}</p>"
            return {}, body.encode("utf8", "replace") if raw else body, \
                "text/html"
        ext = os.path.splitext(local)[1].lower()
        ctype = "text/html" if ext in (".html", ".htm") else "text/plain"
        return {}, payload if raw else payload.decode("utf8", "replace"), ctype

    def _request_data(self, raw=False):
        # data:[<mediatype>][;base64],<data>
        meta, _, data = self.data_payload.partition(",")
        ctype = meta.split(";")[0] or "text/plain"
        if meta.endswith(";base64"):
            decoded = base64.b64decode(data)
        else:
            decoded = urllib.parse.unquote(data)
        if isinstance(decoded, bytes):
            body = decoded if raw else decoded.decode("utf8", "replace")
        else:
            body = decoded.encode("utf8", "replace") if raw else decoded
        return {}, body, ctype

    def _new_connection(self):
        s = _connect(self.host, self.port)
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            try:
                ctx.set_alpn_protocols(["h2", "http/1.1"])
            except (NotImplementedError, ssl.SSLError):
                pass  # a Python/OpenSSL build without ALPN stays HTTP/1.1
            s = ctx.wrap_socket(s, server_hostname=self.host)
        # Guard against a server that streams forever / stalls: bound each
        # recv() call so a dead or hostile peer can't hang the UI.
        s.settimeout(30)
        return s

    def _request_http(self, redirects_left, payload, raw=False, refresh=False):
        # Two documents that differ only by fragment are the same resource.
        # A text fetch and a bytes fetch of the same URL are not the same
        # resource: the document that *is* an image is fetched as text (for
        # its content type) and then re-fetched as bytes (for the <img> that
        # shows it), and serving the text-decoded entry to the bytes caller
        # hands the decoder mangled bytes.
        cache_key = (str(self).split("#", 1)[0], raw)
        if not refresh and payload is None and cache_key in _CACHE:
            expires, entry = _CACHE[cache_key]
            if expires is None or expires > time.time():
                return entry
            else:
                del _CACHE[cache_key]

        method = "POST" if payload is not None else "GET"
        headers = dict(DEFAULT_HEADERS)
        headers["Host"] = self.netloc()  # brackets IPv6, includes the port
        headers["Connection"] = "keep-alive"
        if refresh:
            # Ask intermediaries to revalidate too.
            headers["Cache-Control"] = "no-cache"
        body_bytes = b""
        if payload is not None:
            body_bytes = payload.encode("utf8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Content-Length"] = str(len(body_bytes))

        origin = (self.scheme, self.host, self.port)
        status, resp_headers, body = self._request_transport(
            origin, method, headers, body_bytes)

        # Redirects.
        if status in (301, 302, 303, 307, 308) and "location" in resp_headers:
            if redirects_left <= 0:
                raise RuntimeError("Too many redirects")
            location = resp_headers["location"]
            new_url = self.resolve(location)
            follow_payload = payload if status in (307, 308) else None
            if new_url.scheme in ("http", "https"):
                # Follow in place so `self` reflects the URL we actually got
                # content from (see _adopt); callers resolve relative URLs
                # (img/style/script src) against the page URL, so a bare
                # `google.com` that redirects to `www.google.com` must report
                # the final host or its image URLs resolve to a host that
                # serves HTML instead of the image.
                self._adopt(new_url)
                return self._request_http(redirects_left - 1, follow_payload,
                                          raw=raw, refresh=refresh)
            if new_url.scheme == "file":
                # Never let a remote server redirect into the local
                # filesystem: that would be an arbitrary local-file read with
                # no user interaction.
                raise RuntimeError(
                    f"Blocked redirect to local file: {new_url}")
            return new_url.request(redirects_left - 1, follow_payload, raw=raw,
                                   refresh=refresh)

        # Decode transfer-encoding and content-encoding.
        body = self._decode_body(body, resp_headers)
        charset = self._charset(resp_headers)
        text = body.decode(charset, "replace")
        content_type = resp_headers.get(
            "content-type", "text/html").split(";")[0].strip()

        if raw:
            return (resp_headers, body, content_type)

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
            if "max-age" in cc and len(_CACHE) >= CACHE_MAX_SIZE:
                # Evict expired entries first, then the oldest live one.
                now = time.time()
                for k in [k for k, (exp, _) in _CACHE.items()
                          if exp is not None and exp <= now]:
                    del _CACHE[k]
                if len(_CACHE) >= CACHE_MAX_SIZE and _CACHE:
                    del _CACHE[min(_CACHE, key=lambda k: _CACHE[k][0] or 0)]
            if "max-age" in cc:
                _CACHE[cache_key] = (expires, result)

        return result

    def _request_transport(self, origin, method, headers, body_bytes):
        """Run one request over whatever protocol the server negotiated.

        Returns (status, resp_headers, body). Reuses a multiplexed HTTP/2
        connection when this origin has one, or upgrades a fresh TLS socket to
        HTTP/2 when ALPN negotiated it; everything else stays on HTTP/1.1.
        """
        req = URL._h1_request_bytes(method, self.path, headers, body_bytes)

        def attempt(sock):
            try:
                sock.sendall(req)
            except OSError:
                _close_socket(sock)
                raise
            return self._read_response(sock)

        # HTTP/2: a multiplexed connection already parked for this origin, or
        # a fresh TLS socket whose handshake negotiated h2.
        if self.scheme == "https":
            conn = _h2_take(origin)
            if conn is not None:
                try:
                    return conn.request(method, self.path, headers, body_bytes)
                except (H2Error, OSError):
                    # The multiplexed connection died under us; drop it and
                    # fall through to a fresh one rather than failing.
                    _h2_drop(origin, conn)
            # Reuse an idle HTTP/1.1 socket first; a fresh TLS socket is only
            # opened to learn the negotiated protocol when the pool is empty.
            s = _pool_take(origin)
            pooled = s is not None
            if s is None:
                s = self._new_connection()
                if _alpn_proto(s) == "h2":
                    conn = H2Connection(s, _MAX_BODY_BYTES)
                    try:
                        conn.start()
                        result = conn.request(method, self.path, headers,
                                              body_bytes)
                    except (H2Error, OSError):
                        _close_socket(s)
                        raise
                    _h2_park(origin, conn)
                    return result
        else:
            s = _pool_take(origin)
            pooled = s is not None
            if s is None:
                s = self._new_connection()

        # HTTP/1.1: retry once on a fresh connection if a parked one went
        # stale (the peer closed it, e.g. an HTTP/1.0 server or a keep-alive
        # timeout).
        try:
            try:
                status, resp_headers, body, reusable = attempt(s)
            except (OSError, RuntimeError):
                _close_socket(s)
                if not pooled:
                    raise
                s = self._new_connection()
                status, resp_headers, body, reusable = attempt(s)
            if reusable:
                _pool_park(origin, s)
            else:
                # Body was read to EOF (no framing), so the connection cannot
                # be reused; it is already closed by the peer.
                _close_socket(s)
        except BaseException:
            _close_socket(s)
            raise
        return status, resp_headers, body

    @staticmethod
    def _h1_request_bytes(method, path, headers, body_bytes):
        lines = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
        return (f"{method} {path} HTTP/1.1\r\n{lines}\r\n\r\n"
                .encode("utf8")) + body_bytes

    @staticmethod
    def _read_response(s):
        """Read a full HTTP response, honoring Content-Length / chunked
        framing so a keep-alive peer that does not close the socket doesn't
        make us wait for EOF (which could stall for the socket timeout).

        Returns (status, headers, body_bytes, reusable), where `reusable` is
        True only when the body was read to a clean frame boundary, so the
        caller may park the socket for another request. A read-to-EOF body
        (no framing hints) means the peer closed the connection, so it cannot
        be reused."""
        buf = bytearray()

        def read_more(n=65536):
            if len(buf) >= _MAX_BODY_BYTES:
                return b""
            n = min(n, _MAX_BODY_BYTES - len(buf))
            if n <= 0:
                return b""
            chunk = s.recv(n)
            if chunk:
                buf.extend(chunk)
            return chunk

        # Read the header block (terminated by an empty line).
        while b"\r\n\r\n" not in buf and read_more():
            pass
        if b"\r\n\r\n" not in buf:
            raise RuntimeError("HTTP response headers too large")
        head, _, body = bytes(buf).partition(b"\r\n\r\n")
        headers = URL._parse_headers(head)

        reusable = False
        if headers.get("transfer-encoding", "").lower() == "chunked":
            # Read until the terminating zero-length chunk.
            while b"\r\n0\r\n\r\n" not in buf and read_more():
                pass
            reusable = b"\r\n0\r\n\r\n" in buf
        elif "content-length" in headers:
            try:
                remaining = int(headers["content-length"]) - len(body)
            except ValueError:
                remaining = -1
            if remaining > _MAX_BODY_BYTES:
                raise RuntimeError("HTTP response body too large")
            while remaining > 0:
                chunk = read_more(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
            reusable = remaining <= 0
        else:
            # No framing hints: read until EOF (socket timeout guards stalls).
            while read_more():
                pass

        head, _, body = bytes(buf).partition(b"\r\n\r\n")
        return URL._parse_status(head), headers, body, reusable

    @staticmethod
    def _parse_status(head):
        """Parse the HTTP status code out of a header block."""
        # Status line is: HTTP/x.y <code> <explanation>
        status_line = head.split(b"\r\n")[0].decode("latin1")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise RuntimeError(f"Malformed status line: {status_line!r}")
        return int(parts[1])

    @staticmethod
    def _parse_headers(head):
        headers = {}
        for line in head.split(b"\r\n")[1:]:
            if not line:
                continue
            k, _, v = line.decode("latin1").partition(":")
            headers[k.strip().lower()] = v.strip()
        return headers

    @staticmethod
    def _decompress_gzip_bounded(data):
        """Decompress a gzip payload, refusing output beyond the body cap.

        Uses a streaming zlib object with a max_length limit rather than
        gzip.decompress so a decompression bomb cannot allocate unbounded
        memory; returns None when the output would exceed the cap or the
        stream is malformed."""
        try:
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            out = d.decompress(data, _MAX_BODY_BYTES)
            if d.unconsumed_tail or d.unused_data:
                return None
            return out
        except zlib.error:
            return None

    @staticmethod
    def _decode_body(body, headers):
        if headers.get("transfer-encoding", "").lower() == "chunked":
            body = URL._dechunk(body)
        return URL._decode_content_encoding(body, headers)

    @staticmethod
    def _decode_content_encoding(body, headers, limit=_MAX_BODY_BYTES):
        enc = headers.get("content-encoding", "").lower()
        if enc == "gzip":
            decompressed = URL._decompress_gzip_bounded(body)
            if decompressed is not None:
                body = decompressed
        elif enc == "deflate":
            for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
                try:
                    body = zlib.decompress(body, wbits, limit)
                    break
                except (OSError, ValueError, zlib.error):
                    continue
        return body

    @staticmethod
    def _dechunk(body):
        out = bytearray()
        while body:
            size_line, _, body = body.partition(b"\r\n")
            try:
                size = int(size_line.strip().split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                break
            if len(body) < size + 2:
                break  # truncated chunk
            out += body[:size]
            body = body[size + 2:]  # skip trailing CRLF
        return bytes(out)

    @staticmethod
    def _charset(headers):
        ctype = headers.get("content-type", "")
        if "charset=" in ctype:
            value = ctype.split("charset=", 1)[1].split(";")[0].strip()
            value = value.strip("\"'")  # servers send charset="utf-8" too
            try:
                codecs.lookup(value)
                return value
            except LookupError:
                pass
        return "utf8"


# -- Streaming responses -------------------------------------------------
#
# Everything above reads a whole response into memory before the caller sees
# a byte of it. That is the right shape for a document -- the parser needs
# all of it anyway -- and exactly the wrong one for a file: _MAX_BODY_BYTES
# would reject a 2 GB download, and if it did not, the memory would still
# not be there. What follows hands back the status line and the headers as
# soon as they arrive and leaves the body on the socket, so a download can
# write it to disk a piece at a time (feetbrowser/downloads.py) and a
# navigation can decide from the headers alone whether what is coming back
# is a page at all. It is deliberately separate from _request_http: no
# cache, no connection pool, and no decompressing a body nobody has read.

#: Cap on the header block of a streamed response. The body is unbounded by
#: design here; the headers are not.
_MAX_HEAD_BYTES = 256 * 1024


class IncompleteRead(OSError):
    """The peer stopped sending before the framing said the body ended.

    An OSError, so a caller that already handles "the network broke" handles
    a truncated body the same way -- which is what it is. It carries how
    much arrived, so a download can decide whether resuming is worth a try.
    """

    def __init__(self, message, received=0, expected=None):
        super().__init__(message)
        self.received = received
        self.expected = expected


class HTTPStream:
    """A response whose headers have arrived and whose body has not.

    Iterate `chunks()` to pull the body off the socket a piece at a time,
    already stripped of chunked transfer-encoding. `length` is the body size
    when the server stated one and None when it did not, which is a fact the
    caller has to carry rather than paper over: there is no honest
    percentage for a response of unknown length.
    """

    def __init__(self, url, sock, status, headers, leftover=b""):
        self.url = url
        self.status = status
        self.headers = headers
        self._sock = sock
        self._buf = bytearray(leftover)
        self._eof = False
        self._closed = False
        self.received = 0
        te = headers.get("transfer-encoding", "").lower()
        self.chunked = "chunked" in te
        self.length = None
        if not self.chunked:
            try:
                self.length = int(headers["content-length"])
            except (KeyError, ValueError):
                self.length = None

    # -- introspection ---------------------------------------------------

    @property
    def content_type(self):
        return self.headers.get(
            "content-type", "").split(";")[0].strip().lower()

    def charset(self):
        return URL._charset(self.headers)

    # -- reading ---------------------------------------------------------

    def _fill(self, n=65536):
        """Pull more bytes off the socket. False once the peer is done."""
        if self._eof:
            return False
        data = self._sock.recv(max(1, min(n, 65536)))
        if not data:
            self._eof = True
            return False
        self._buf.extend(data)
        return True

    def _take(self, n):
        piece = bytes(self._buf[:n])
        del self._buf[:n]
        self.received += len(piece)
        return piece

    def chunks(self, size=65536):
        """Yield the body a piece at a time, transfer-encoding removed."""
        if self.chunked:
            return self._chunked(size)
        if self.length is not None:
            return self._counted(size)
        return self._until_eof(size)

    def _counted(self, size):
        remaining = self.length
        while remaining > 0:
            if not self._buf and not self._fill(min(size, remaining)):
                raise IncompleteRead(
                    "connection closed with %d of %d bytes received"
                    % (self.received, self.length),
                    received=self.received, expected=self.length)
            take = min(len(self._buf), remaining)
            remaining -= take
            yield self._take(take)

    def _until_eof(self, size):
        # No framing at all: EOF is the end of the body, and nothing here
        # can tell a complete response from a truncated one.
        while True:
            if self._buf:
                yield self._take(len(self._buf))
            elif not self._fill(size):
                return

    def _chunked(self, size):
        while True:
            while b"\r\n" not in self._buf:
                if not self._fill(size):
                    raise IncompleteRead("truncated chunked body",
                                         received=self.received)
            line, _, _rest = bytes(self._buf).partition(b"\r\n")
            del self._buf[:len(line) + 2]
            try:
                count = int(line.strip().split(b";")[0], 16)
            except ValueError:
                raise IncompleteRead("malformed chunk header: %r" % line[:40],
                                     received=self.received)
            if count == 0:
                return  # trailers, then the blank line; nothing reads them
            got = 0
            while got < count:
                if not self._buf and not self._fill(min(size, count - got)):
                    raise IncompleteRead("truncated chunk",
                                         received=self.received)
                take = min(len(self._buf), count - got)
                got += take
                yield self._take(take)
            while len(self._buf) < 2:
                if not self._fill(size):
                    raise IncompleteRead("chunk missing its terminator",
                                         received=self.received)
            del self._buf[:2]

    def read_all(self, limit=_MAX_BODY_BYTES, decode=True):
        """Read the whole body into memory, bounded by `limit`.

        For the caller that turned out to want a document after all: the
        same cap and the same content-encoding handling as request(), so a
        page fetched through a stream is the same string a page fetched
        through _request_http would have been.
        """
        out = bytearray()
        for piece in self.chunks():
            out.extend(piece)
            if len(out) > limit:
                raise RuntimeError("HTTP response body too large")
        body = bytes(out)
        if decode:
            body = URL._decode_content_encoding(body, self.headers, limit)
        return body

    # -- teardown --------------------------------------------------------

    def shutdown(self):
        """Unblock a read in progress from another thread.

        A cancelled download has to stop a worker that may be parked in
        recv() until the socket timeout. Shutting the socket down makes that
        recv return at once without closing the descriptor underneath the
        thread that owns it -- close() from a second thread frees a number
        the kernel is free to hand to the next file opened anywhere in the
        process.
        """
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def close(self):
        if not self._closed:
            self._closed = True
            _close_socket(self._sock)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _read_head(sock):
    """Read just the header block. Returns (status, headers, leftover)."""
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        if len(buf) > _MAX_HEAD_BYTES:
            raise RuntimeError("HTTP response headers too large")
        data = sock.recv(65536)
        if not data:
            raise IncompleteRead("connection closed before the headers ended")
        buf.extend(data)
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    return URL._parse_status(head), URL._parse_headers(head), rest


def open_stream(url, extra_headers=None, timeout=30,
                redirects_left=MAX_REDIRECTS, accept_encoding="identity"):
    """Send a GET and return an HTTPStream positioned at the body.

    Redirects are followed here, so the caller only ever sees the response
    it is going to read, and a redirect out of http/https is refused for the
    same reason _request_http refuses one: a remote server must not be able
    to point us at the local filesystem. `accept_encoding` defaults to
    identity because a download wants the bytes of the file, and a
    Content-Length describing something else is worse than none at all.
    """
    if isinstance(url, str):
        url = URL(url)
    while True:
        if url.scheme not in ("http", "https"):
            raise ValueError(f"cannot stream a {url.scheme or '?'}: URL")
        headers = dict(DEFAULT_HEADERS)
        headers["Host"] = url.netloc()
        headers["Accept-Encoding"] = accept_encoding
        headers["Connection"] = "close"
        if extra_headers:
            headers.update(extra_headers)
        sock = _connect(url.host, url.port)
        try:
            if url.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=url.host)
            sock.settimeout(timeout)
            lines = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
            sock.sendall(
                f"GET {url.path} HTTP/1.1\r\n{lines}\r\n\r\n".encode("utf8"))
            status, resp_headers, leftover = _read_head(sock)
        except BaseException:
            _close_socket(sock)
            raise
        if status in (301, 302, 303, 307, 308) and "location" in resp_headers:
            _close_socket(sock)
            if redirects_left <= 0:
                raise RuntimeError("Too many redirects")
            redirects_left -= 1
            target = url.resolve(resp_headers["location"])
            if target.scheme not in ("http", "https"):
                raise RuntimeError(f"Blocked redirect to {target}")
            url = target
            continue
        return HTTPStream(url, sock, status, resp_headers, leftover)
