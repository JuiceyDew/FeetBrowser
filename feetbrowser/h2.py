"""HTTP/2 framing and connection handling for FeetBrowser (RFC 7540).

This is a from-scratch HTTP/2 client: the 9-byte frame header, the frame
types a client needs, flow control, the connection preface, and a multiplexed
reader that hands each stream its own response. Header compression is HPACK
(feetbrowser.hpack). No third-party HTTP/2 library is involved.

The connection owns the socket. `H2Connection` runs a daemon reader thread
that parses frames and dispatches them to per-stream buffers; callers open a
stream with `request()` and block until that stream's response is complete,
while other streams keep flowing in the background. That multiplexing is the
point of HTTP/2: a page's dozens of resources share one TLS connection.
"""

import struct
import threading

from feetbrowser.hpack import (
    DynamicTable,
    HpackError,
    decode_header_block,
    encode_header_block,
)

# Frame types (RFC 7540 Section 6).
DATA = 0x0
HEADERS = 0x1
PRIORITY = 0x2
RST_STREAM = 0x3
SETTINGS = 0x4
PUSH_PROMISE = 0x5
PING = 0x6
GOAWAY = 0x7
WINDOW_UPDATE = 0x8
CONTINUATION = 0x9

# Frame flags.
_FLAG_END_STREAM = 0x1
_FLAG_ACK = 0x1
_FLAG_END_HEADERS = 0x4
_FLAG_PADDED = 0x8
_FLAG_PRIORITY = 0x20

# Settings identifiers (RFC 7540 Section 6.5.2).
SETTINGS_HEADER_TABLE_SIZE = 0x1
SETTINGS_ENABLE_PUSH = 0x2
SETTINGS_MAX_CONCURRENT_STREAMS = 0x3
SETTINGS_INITIAL_WINDOW_SIZE = 0x4
SETTINGS_MAX_FRAME_SIZE = 0x5
SETTINGS_MAX_HEADER_LIST_SIZE = 0x6

#: The client connection preface (RFC 7540 Section 3.5). Sent verbatim as the
#: first bytes on a new connection.
PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"

#: Default per-stream and connection flow-control windows (RFC 7540
#: Section 6.9.2). The reader replenishes them as DATA arrives.
DEFAULT_WINDOW = 65535

#: The header-table size we advertise. OUR_SETTINGS omits
#: SETTINGS_HEADER_TABLE_SIZE, so the default applies and the peer may not
#: grow its encoder table past this.
OUR_HEADER_TABLE_SIZE = 4096

#: The largest frame we accept. OUR_SETTINGS omits SETTINGS_MAX_FRAME_SIZE,
#: so this default is what we advertise and must enforce on inbound frames.
OUR_MAX_FRAME_SIZE = 16384

#: Overall deadline for one request/response, so a peer that keeps dribbling
#: partial frame data can stall a stream forever only for so long. The socket
#: timeout only bounds each recv(); this bounds the whole exchange.
RESPONSE_TIMEOUT = 300.0

#: Our settings. ENABLE_PUSH 0 tells the server not to push resources at us
#: (we would refuse them anyway); a large initial window avoids stalling on
#: pages with big bodies before the reader's WINDOW_UPDATEs take effect.
OUR_SETTINGS = {
    SETTINGS_ENABLE_PUSH: 0,
    SETTINGS_INITIAL_WINDOW_SIZE: 16 * 1024 * 1024,
}


class H2Error(IOError):
    """The HTTP/2 connection is unusable (protocol error or peer closed it)."""


class Stream:
    """One multiplexed HTTP/2 request/response exchange.

    `response()` blocks until the response headers and body have arrived (or
    the stream is reset / the connection dies), then returns the body bytes.
    The status and headers are filled in by the connection's reader thread;
    callers read them after `response()` returns.
    """

    def __init__(self, stream_id, send_window):
        self.id = stream_id
        self.status = 0
        self.headers = []  # list of (name, value) bytes pairs
        self.body = bytearray()
        self.send_window = send_window  # how much body this stream may still send
        self._done = threading.Event()
        self._error = None

    def finish(self, error=None):
        # The stream's outcome is decided by the first terminal event. A peer
        # that answers then closes can race a successful finish against
        # _fail_all; the connection error must not overwrite the response we
        # already have.
        if self._done.is_set():
            return
        self._error = error
        self._done.set()

    def wait(self, timeout=None):
        """Block until the response completes, or raise the stream's error.
        `timeout` is an overall deadline (None blocks forever)."""
        if not self._done.wait(timeout):
            raise H2Error("HTTP/2 response timed out")
        if self._error is not None:
            raise self._error

    def response(self, limit, timeout=None):
        """Block for the full response; return the decoded body bytes."""
        self.wait(timeout)
        body = bytes(self.body)
        if len(body) > limit:
            raise RuntimeError("HTTP/2 response body too large")
        return body


class H2Connection:
    """A single HTTP/2 connection: own the socket, negotiate, multiplex.

    `request(method, path, headers, body)` sends one request on a fresh
    stream and returns (status, headers_dict, body_bytes). Streams are served
    concurrently by a reader thread, so N requests can be in flight at once;
    each blocks only on its own response.

    The connection stays usable until `close()` or until the peer sends
    GOAWAY / the socket dies (marked by `dead`). Reuse across requests is the
    caller's business: nothing here closes or reopens the socket.
    """

    def __init__(self, sock, max_body_bytes, timeout=RESPONSE_TIMEOUT):
        self._sock = sock
        self._max_body_bytes = max_body_bytes
        self._response_timeout = timeout
        self._lock = threading.Lock()  # guards sends
        self._flow_cv = threading.Condition(self._lock)  # flow control / capacity
        self._streams = {}
        self._pending_headers = {}  # stream id -> (bytearray, END_STREAM flag)
        self._next_stream_id = 1  # client-initiated streams are odd
        self._decoder_table = DynamicTable()
        self._encoder_table = DynamicTable()
        # Server settings we have received (defaults per RFC 7540 6.5.2).
        self._peer_header_table_size = 4096  # what we may use to encode
        self._peer_max_streams = None  # None = unlimited
        self._max_frame_size = 16384  # the frame size the peer lets us send
        self._our_max_frame_size = OUR_MAX_FRAME_SIZE  # what we accept
        self._initial_window = DEFAULT_WINDOW  # per-stream send window
        self._conn_send_window = DEFAULT_WINDOW  # connection send window
        self._conn_recv_window = DEFAULT_WINDOW  # how much peer may send us
        self._dead = False
        self._goaway = False
        self._read_thread = None
        self._preface_sent = False

    # -- lifecycle -----------------------------------------------------

    def start(self):
        """Send the connection preface and our SETTINGS, then start the
        reader thread. Must be called once before any request."""
        with self._lock:
            try:
                self._sock.sendall(PREFACE)
                self._sock.sendall(self._frame(SETTINGS, 0, 0,
                                               self._pack_settings(OUR_SETTINGS)))
            except OSError as exc:
                self._dead = True
                raise H2Error(f"HTTP/2 handshake failed: {exc}") from exc
            self._preface_sent = True
        self._read_thread = threading.Thread(
            target=self._read_loop, name="h2-read", daemon=True)
        self._read_thread.start()

    def close(self):
        self._dead = True
        try:
            self._sock.close()
        except OSError:
            pass
        with self._flow_cv:
            self._flow_cv.notify_all()

    @property
    def dead(self):
        return self._dead

    # -- framing -------------------------------------------------------

    @staticmethod
    def _frame(ftype, flags, stream_id, payload=b""):
        return (len(payload).to_bytes(3, "big")
                + bytes((ftype, flags))
                + struct.pack("!I", stream_id & 0x7FFFFFFF)
                + payload)

    @staticmethod
    def _pack_settings(settings):
        out = bytearray()
        for key, value in settings.items():
            out += struct.pack("!HI", key, value)
        return bytes(out)

    @staticmethod
    def _unpack_settings(payload):
        if len(payload) % 6:
            raise H2Error("malformed SETTINGS frame")
        settings = {}
        for i in range(0, len(payload), 6):
            key, value = struct.unpack("!HI", payload[i:i + 6])
            settings[key] = value
        return settings

    # -- sending -------------------------------------------------------

    def _send(self, data):
        if self._dead:
            raise H2Error("HTTP/2 connection is closed")
        with self._lock:
            try:
                self._sock.sendall(data)
            except OSError as exc:
                self._dead = True
                raise H2Error(f"HTTP/2 write failed: {exc}") from exc

    def request(self, method, path, headers, body=b"", refresh=False,
                timeout=None):
        """Open a stream and send one request. Returns
        (status, headers_dict, body_bytes)."""
        if self._dead:
            raise H2Error("HTTP/2 connection is closed")
        if not self._preface_sent:
            self.start()
        # The peer may cap how many streams it will have open at once; wait
        # for capacity instead of opening a stream it will just reset.
        if self._peer_max_streams is not None:
            with self._flow_cv:
                while not self._dead and \
                        len(self._streams) >= self._peer_max_streams:
                    self._flow_cv.wait()
                if self._dead:
                    raise H2Error("HTTP/2 connection is closed")
        with self._lock:
            # Stream ids are 31-bit odd numbers; past the top of the range the
            # identifier would wrap (the reserved bit is masked in _frame) and
            # silently reuse a live id, so a connection this old is done.
            if self._next_stream_id > 0x7FFFFFFF:
                self._dead = True
                raise H2Error("HTTP/2 stream ids exhausted")
            stream_id = self._next_stream_id
            self._next_stream_id += 2
            stream = Stream(stream_id, self._initial_window)
            self._streams[stream_id] = stream

        # HPACK header block (pseudo-headers first, per RFC 7540 8.1.2.1).
        block = self._build_block(method, path, headers)

        flags = _FLAG_END_HEADERS | (_FLAG_END_STREAM if not body else 0)
        self._send(self._frame(HEADERS, flags, stream_id, block))
        if body:
            # Split the body to the peer's max frame size and respect its
            # flow-control window: a frame larger than SETTINGS_MAX_FRAME_SIZE
            # is a connection error, and overrunning the window stalls the
            # transfer. END_STREAM rides the final DATA frame.
            limit = self._max_frame_size
            for i in range(0, len(body), limit):
                piece = body[i:i + limit]
                last = i + len(piece) >= len(body)
                self._send_data(stream_id, piece, last)
        try:
            body_bytes = stream.response(self._max_body_bytes,
                                         timeout or self._response_timeout)
        except BaseException:
            self._streams.pop(stream_id, None)
            with self._flow_cv:
                self._flow_cv.notify_all()
            raise
        self._streams.pop(stream_id, None)
        with self._flow_cv:
            self._flow_cv.notify_all()
        status = stream.status
        resp_headers = {}
        for k, v in stream.headers:
            if not k.startswith(b":"):
                resp_headers[k.decode("latin1")] = v.decode("latin1")
        return status, resp_headers, body_bytes

    def _send_data(self, stream_id, piece, last):
        """Send one DATA frame, waiting for flow-control credit first."""
        with self._flow_cv:
            stream = self._streams.get(stream_id)
            while not self._dead and (
                    self._conn_send_window < len(piece)
                    or stream is not None
                    and stream.send_window < len(piece)):
                self._flow_cv.wait()
            if self._dead:
                raise H2Error("HTTP/2 connection is closed")
            self._conn_send_window -= len(piece)
            if stream is not None:
                stream.send_window -= len(piece)
        self._send(self._frame(DATA, _FLAG_END_STREAM if last else 0,
                               stream_id, piece))

    def _build_block(self, method, path, headers):
        pseudo = [
            (b":method", method.encode("latin1")),
            (b":scheme", b"https"),
            (b":path", path.encode("latin1")),
            (b":authority", self._host_header(headers).encode("latin1")),
        ]
        # Connection-specific headers are forbidden in HTTP/2 (RFC 7540
        # 8.1.2.2); Host becomes :authority above.
        forbidden = {"connection", "keep-alive", "proxy-connection",
                     "transfer-encoding", "upgrade", "host"}
        regular = [(k.lower().encode("latin1"), v.encode("latin1"))
                   for k, v in headers.items()
                   if k.lower() not in forbidden]
        return encode_header_block(pseudo + regular, self._encoder_table)

    @staticmethod
    def _host_header(headers):
        for key, value in headers.items():
            if key.lower() == "host":
                return value
        return ""

    # -- reader --------------------------------------------------------

    def _read_loop(self):
        buf = bytearray()
        try:
            while not self._dead:
                frame = self._read_frame(buf)
                if frame is None:
                    break
                self._handle_frame(*frame)
        except (H2Error, HpackError) as exc:
            self._dead = True
            self._fail_all(exc)
        except (OSError, struct.error, ValueError) as exc:
            self._dead = True
            self._fail_all(H2Error(f"HTTP/2 read failed: {exc}"))
        except BaseException:
            self._dead = True
            self._fail_all(H2Error("HTTP/2 connection failed"))
        if not self._dead:
            self._dead = True
            self._fail_all(H2Error("HTTP/2 peer closed the connection"))

    def _read_frame(self, buf):
        """Pull one full frame from the socket into `buf`, then return
        (ftype, flags, stream_id, payload). Returns None at EOF."""
        while len(buf) < 9:
            chunk = self._sock.recv(65536)
            if not chunk:
                return None
            buf.extend(chunk)
        length = int.from_bytes(buf[0:3], "big")
        ftype, flags = buf[3], buf[4]
        stream_id = struct.unpack("!I", buf[5:9])[0] & 0x7FFFFFFF
        del buf[:9]
        # Reject oversized frames from the header alone, before reading the
        # (possibly up to 2^24-1 byte) payload into memory. The limit is what
        # we advertise -- self._max_frame_size is the value the peer sent us
        # and constrains what we may send, not what we accept.
        if length > self._our_max_frame_size:
            raise H2Error("frame larger than our SETTINGS_MAX_FRAME_SIZE")
        while len(buf) < length:
            chunk = self._sock.recv(65536)
            if not chunk:
                return None
            buf.extend(chunk)
        payload = bytes(buf[:length])
        del buf[:length]
        return ftype, flags, stream_id, payload

    def _handle_frame(self, ftype, flags, stream_id, payload):
        if ftype == DATA:
            self._handle_data(stream_id, flags, payload)
        elif ftype == HEADERS:
            self._handle_headers(stream_id, flags, payload)
        elif ftype == CONTINUATION:
            self._handle_continuation(stream_id, flags, payload)
        elif ftype == SETTINGS:
            self._handle_settings(flags, payload)
        elif ftype == PING:
            if not flags & _FLAG_ACK:
                self._send(self._frame(PING, _FLAG_ACK, 0, payload))
        elif ftype == RST_STREAM:
            self._handle_rst(stream_id)
        elif ftype == WINDOW_UPDATE:
            self._handle_window_update(stream_id, payload)
        elif ftype == GOAWAY:
            self._dead = True
            self._fail_all(H2Error("peer sent GOAWAY"))
        elif ftype == PRIORITY:
            # PRIORITY carries nothing we use.
            pass
        elif ftype == PUSH_PROMISE:
            # We advertise ENABLE_PUSH=0, so PUSH_PROMISE should not arrive.
            self._dead = True
            self._fail_all(H2Error("peer sent PUSH_PROMISE"))
        else:
            # Frames of unknown type must be discarded, not fail the
            # connection (RFC 7540 Section 4.1 and 5.5); deployed servers
            # send extension frames like ALTSVC on ordinary connections.
            pass

    def _handle_settings(self, flags, payload):
        if flags & _FLAG_ACK:
            return
        settings = self._unpack_settings(payload)
        for key, value in settings.items():
            if key == SETTINGS_HEADER_TABLE_SIZE:
                # The size the peer lets us use for encoding. Keep the limit
                # on what we decode (OUR_HEADER_TABLE_SIZE) separate, and
                # resize the encoder table; encode_header_block emits the
                # required size update on the next block.
                self._peer_header_table_size = value
                self._encoder_table.resize(value)
            elif key == SETTINGS_MAX_FRAME_SIZE:
                if value < 16384:
                    raise H2Error("SETTINGS_MAX_FRAME_SIZE below minimum")
                self._max_frame_size = value
            elif key == SETTINGS_MAX_CONCURRENT_STREAMS:
                self._peer_max_streams = value
            elif key == SETTINGS_INITIAL_WINDOW_SIZE:
                if value > 0x7FFFFFFF:
                    raise H2Error("SETTINGS_INITIAL_WINDOW_SIZE too large")
                # The setting changes the per-stream default and adjusts the
                # windows of already-open streams by the delta (RFC 7540
                # Section 6.9.2); the connection window is unaffected.
                delta = value - self._initial_window
                self._initial_window = value
                if delta:
                    with self._flow_cv:
                        for stream in self._streams.values():
                            stream.send_window += delta
                        self._flow_cv.notify_all()
        self._send(self._frame(SETTINGS, _FLAG_ACK, 0, b""))

    def _handle_data(self, stream_id, flags, payload):
        stream = self._streams.get(stream_id)
        if stream is None:
            self._rst(stream_id)
            return
        stream.body.extend(payload)
        if len(stream.body) > self._max_body_bytes:
            stream.finish(RuntimeError("HTTP/2 response body too large"))
            self._rst(stream_id)
            return
        # Replenish both the stream and the connection receive windows so the
        # peer can keep sending.
        if payload:
            self._conn_recv_window -= len(payload)
            if self._conn_recv_window <= 0:
                self._send(self._frame(WINDOW_UPDATE, 0, 0,
                                       len(payload).to_bytes(4, "big")))
                self._conn_recv_window += len(payload)
            self._send(self._frame(WINDOW_UPDATE, 0, stream_id,
                                   len(payload).to_bytes(4, "big")))
        if flags & _FLAG_END_STREAM:
            stream.finish()

    def _handle_headers(self, stream_id, flags, payload):
        stream = self._streams.get(stream_id)
        if stream is None:
            self._rst(stream_id)
            return
        block = self._header_block(flags, payload)
        if not flags & _FLAG_END_HEADERS:
            # The header block continues on CONTINUATION frames; buffer the
            # fragment (plus the END_STREAM flag) until it completes.
            self._pending_headers[stream_id] = (
                bytearray(block), flags & _FLAG_END_STREAM)
            return
        self._finish_headers(stream_id, flags, block)

    def _handle_continuation(self, stream_id, flags, payload):
        pending = self._pending_headers.get(stream_id)
        if pending is None:
            raise H2Error("unexpected CONTINUATION frame")
        buf, end_stream = pending
        buf.extend(payload)
        if not flags & _FLAG_END_HEADERS:
            return
        del self._pending_headers[stream_id]
        self._finish_headers(stream_id, flags | end_stream, buf)

    @staticmethod
    def _header_block(flags, payload):
        """Strip the Pad Length and PRIORITY fields off a HEADERS payload,
        returning the header-block fragment. The padding is validated so a
        malformed peer cannot make us slice nonsense."""
        pos = 0
        end = len(payload)
        pad_len = 0
        if flags & _FLAG_PADDED:
            if not payload:
                raise H2Error("padded HEADERS with no pad length")
            pad_len = payload[0]
            pos = 1
        if flags & _FLAG_PRIORITY:
            pos += 5
        if pos > end:
            raise H2Error("HEADERS priority/padding overruns payload")
        if pad_len > end - pos:
            raise H2Error("invalid HEADERS padding")
        return payload[pos:end - pad_len]

    def _finish_headers(self, stream_id, flags, block):
        stream = self._streams.get(stream_id)
        if stream is None:
            self._rst(stream_id)
            return
        headers = decode_header_block(block, self._decoder_table,
                                      OUR_HEADER_TABLE_SIZE)
        stream.headers = headers
        for name, value in headers:
            if name == b":status":
                stream.status = int(value)
        if flags & _FLAG_END_STREAM:
            stream.finish()

    def _handle_rst(self, stream_id):
        self._pending_headers.pop(stream_id, None)
        stream = self._streams.get(stream_id)
        if stream is not None:
            stream.finish(H2Error(f"stream {stream_id} reset by peer"))

    def _handle_window_update(self, stream_id, payload):
        if len(payload) != 4:
            raise H2Error("malformed WINDOW_UPDATE")
        increment = struct.unpack("!I", payload)[0] & 0x7FFFFFFF
        with self._flow_cv:
            if stream_id == 0:
                self._conn_send_window += increment
            else:
                stream = self._streams.get(stream_id)
                if stream is not None:
                    stream.send_window += increment
            # Wake any sender blocked on flow-control credit.
            self._flow_cv.notify_all()

    def _rst(self, stream_id):
        self._send(self._frame(RST_STREAM, 0, stream_id,
                               struct.pack("!I", 7)))  # REFUSED_STREAM

    def _fail_all(self, error):
        for stream in list(self._streams.values()):
            stream.finish(error)
        self._streams.clear()
        self._pending_headers.clear()
        # Wake request threads waiting on flow control or stream capacity so
        # they see the connection is dead instead of waiting forever.
        with self._flow_cv:
            self._flow_cv.notify_all()