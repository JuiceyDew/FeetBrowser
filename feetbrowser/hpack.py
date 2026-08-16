"""HPACK: Header Compression for HTTP/2 (RFC 7541).

A from-scratch encoder/decoder for the header compression scheme used by
HTTP/2. Nothing here wraps a third-party hpack library; the static table,
the dynamic table, the variable-length integer encoding and the canonical
Huffman code (Appendix B of the RFC) are all implemented directly.

The Huffman table below is reproduced verbatim from RFC 7541 Appendix B as
(code, nbits) pairs indexed by symbol (0..255, with EOS at 256). Each code is
the symbol's Huffman code as a base-2 integer aligned on the most significant
bit, which is exactly the shape needed to bit-pack an encoder output stream.
"""

#: Huffman code table, symbol -> (code aligned to MSB, number of bits).
#: Symbol 256 is EOS; it never appears in a valid string literal but its
#: leading bits are the padding appended to align the last byte.
HUFFMAN_CODE = (
    (0x1FF8, 13), (0x7FFFD8, 23), (0xFFFFFE2, 28), (0xFFFFFE3, 28),
    (0xFFFFFE4, 28), (0xFFFFFE5, 28), (0xFFFFFE6, 28), (0xFFFFFE7, 28),
    (0xFFFFFE8, 28), (0xFFFFEA, 24), (0x3FFFFFFC, 30), (0xFFFFFE9, 28),
    (0xFFFFFEA, 28), (0x3FFFFFFD, 30), (0xFFFFFEB, 28), (0xFFFFFEC, 28),
    (0xFFFFFED, 28), (0xFFFFFEE, 28), (0xFFFFFEF, 28), (0xFFFFFF0, 28),
    (0xFFFFFF1, 28), (0xFFFFFF2, 28), (0x3FFFFFFE, 30), (0xFFFFFF3, 28),
    (0xFFFFFF4, 28), (0xFFFFFF5, 28), (0xFFFFFF6, 28), (0xFFFFFF7, 28),
    (0xFFFFFF8, 28), (0xFFFFFF9, 28), (0xFFFFFFA, 28), (0xFFFFFFB, 28),
    (0x14, 6), (0x3F8, 10), (0x3F9, 10), (0xFFA, 12), (0x1FF9, 13),
    (0x15, 6), (0xF8, 8), (0x7FA, 11), (0x3FA, 10), (0x3FB, 10),
    (0xF9, 8), (0x7FB, 11), (0xFA, 8), (0x16, 6), (0x17, 6), (0x18, 6),
    (0x0, 5), (0x1, 5), (0x2, 5), (0x19, 6), (0x1A, 6), (0x1B, 6),
    (0x1C, 6), (0x1D, 6), (0x1E, 6), (0x1F, 6), (0x5C, 7), (0xFB, 8),
    (0x7FFC, 15), (0x20, 6), (0xFFB, 12), (0x3FC, 10), (0x1FFA, 13),
    (0x21, 6), (0x5D, 7), (0x5E, 7), (0x5F, 7), (0x60, 7), (0x61, 7),
    (0x62, 7), (0x63, 7), (0x64, 7), (0x65, 7), (0x66, 7), (0x67, 7),
    (0x68, 7), (0x69, 7), (0x6A, 7), (0x6B, 7), (0x6C, 7), (0x6D, 7),
    (0x6E, 7), (0x6F, 7), (0x70, 7), (0x71, 7), (0x72, 7), (0xFC, 8),
    (0x73, 7), (0xFD, 8), (0x1FFB, 13), (0x7FFF0, 19), (0x1FFC, 13),
    (0x3FFC, 14), (0x22, 6), (0x7FFD, 15), (0x3, 5), (0x23, 6), (0x4, 5),
    (0x24, 6), (0x5, 5), (0x25, 6), (0x26, 6), (0x27, 6), (0x6, 5),
    (0x74, 7), (0x75, 7), (0x28, 6), (0x29, 6), (0x2A, 6), (0x7, 5),
    (0x2B, 6), (0x76, 7), (0x2C, 6), (0x8, 5), (0x9, 5), (0x2D, 6),
    (0x77, 7), (0x78, 7), (0x79, 7), (0x7A, 7), (0x7B, 7), (0x7FFE, 15),
    (0x7FC, 11), (0x3FFD, 14), (0x1FFD, 13), (0xFFFFFFC, 28), (0xFFFE6, 20),
    (0x3FFFD2, 22), (0xFFFE7, 20), (0xFFFE8, 20), (0x3FFFD3, 22),
    (0x3FFFD4, 22), (0x3FFFD5, 22), (0x7FFFD9, 23), (0x3FFFD6, 22),
    (0x7FFFDA, 23), (0x7FFFDB, 23), (0x7FFFDC, 23), (0x7FFFDD, 23),
    (0x7FFFDE, 23), (0xFFFFEB, 24), (0x7FFFDF, 23), (0xFFFFEC, 24),
    (0xFFFFED, 24), (0x3FFFD7, 22), (0x7FFFE0, 23), (0xFFFFEE, 24),
    (0x7FFFE1, 23), (0x7FFFE2, 23), (0x7FFFE3, 23), (0x7FFFE4, 23),
    (0x1FFFDC, 21), (0x3FFFD8, 22), (0x7FFFE5, 23), (0x3FFFD9, 22),
    (0x7FFFE6, 23), (0x7FFFE7, 23), (0xFFFFEF, 24), (0x3FFFDA, 22),
    (0x1FFFDD, 21), (0xFFFE9, 20), (0x3FFFDB, 22), (0x3FFFDC, 22),
    (0x7FFFE8, 23), (0x7FFFE9, 23), (0x1FFFDE, 21), (0x7FFFEA, 23),
    (0x3FFFDD, 22), (0x3FFFDE, 22), (0xFFFFF0, 24), (0x1FFFDF, 21),
    (0x3FFFDF, 22), (0x7FFFEB, 23), (0x7FFFEC, 23), (0x1FFFE0, 21),
    (0x1FFFE1, 21), (0x3FFFE0, 22), (0x1FFFE2, 21), (0x7FFFED, 23),
    (0x3FFFE1, 22), (0x7FFFEE, 23), (0x7FFFEF, 23), (0xFFFEA, 20),
    (0x3FFFE2, 22), (0x3FFFE3, 22), (0x3FFFE4, 22), (0x7FFFF0, 23),
    (0x3FFFE5, 22), (0x3FFFE6, 22), (0x7FFFF1, 23), (0x3FFFFE0, 26),
    (0x3FFFFE1, 26), (0xFFFEB, 20), (0x7FFF1, 19), (0x3FFFE7, 22),
    (0x7FFFF2, 23), (0x3FFFE8, 22), (0x1FFFFEC, 25), (0x3FFFFE2, 26),
    (0x3FFFFE3, 26), (0x3FFFFE4, 26), (0x7FFFFDE, 27), (0x7FFFFDF, 27),
    (0x3FFFFE5, 26), (0xFFFFF1, 24), (0x1FFFFED, 25), (0x7FFF2, 19),
    (0x1FFFE3, 21), (0x3FFFFE6, 26), (0x7FFFFE0, 27), (0x7FFFFE1, 27),
    (0x3FFFFE7, 26), (0x7FFFFE2, 27), (0xFFFFF2, 24), (0x1FFFE4, 21),
    (0x1FFFE5, 21), (0x3FFFFE8, 26), (0x3FFFFE9, 26), (0xFFFFFFD, 28),
    (0x7FFFFE3, 27), (0x7FFFFE4, 27), (0x7FFFFE5, 27), (0xFFFEC, 20),
    (0xFFFFF3, 24), (0xFFFED, 20), (0x1FFFE6, 21), (0x3FFFE9, 22),
    (0x1FFFE7, 21), (0x1FFFE8, 21), (0x7FFFF3, 23), (0x3FFFEA, 22),
    (0x3FFFEB, 22), (0x1FFFFEE, 25), (0x1FFFFEF, 25), (0xFFFFF4, 24),
    (0xFFFFF5, 24), (0x3FFFFEA, 26), (0x7FFFF4, 23), (0x3FFFFEB, 26),
    (0x7FFFFE6, 27), (0x3FFFFEC, 26), (0x3FFFFED, 26), (0x7FFFFE7, 27),
    (0x7FFFFE8, 27), (0x7FFFFE9, 27), (0x7FFFFEA, 27), (0x7FFFFEB, 27),
    (0xFFFFFFE, 28), (0x7FFFFEC, 27), (0x7FFFFED, 27), (0x7FFFFEE, 27),
    (0x7FFFFEF, 27), (0x7FFFFF0, 27), (0x3FFFFEE, 26), (0x3FFFFFFF, 30),
)

#: The static table (RFC 7541 Appendix A), index 1..61. Index 0 does not
#: exist; it is reserved to mean "the name is a literal" in the literal forms.
STATIC_TABLE = [
    (":authority", ""),
    (":method", "GET"),
    (":method", "POST"),
    (":path", "/"),
    (":path", "/index.html"),
    (":scheme", "http"),
    (":scheme", "https"),
    (":status", "200"),
    (":status", "204"),
    (":status", "206"),
    (":status", "304"),
    (":status", "400"),
    (":status", "404"),
    (":status", "500"),
    ("accept-charset", ""),
    ("accept-encoding", "gzip, deflate"),
    ("accept-language", ""),
    ("accept-ranges", ""),
    ("accept", ""),
    ("access-control-allow-origin", ""),
    ("age", ""),
    ("allow", ""),
    ("authorization", ""),
    ("cache-control", ""),
    ("content-disposition", ""),
    ("content-encoding", ""),
    ("content-language", ""),
    ("content-length", ""),
    ("content-location", ""),
    ("content-range", ""),
    ("content-type", ""),
    ("cookie", ""),
    ("date", ""),
    ("etag", ""),
    ("expect", ""),
    ("expires", ""),
    ("from", ""),
    ("host", ""),
    ("if-match", ""),
    ("if-modified-since", ""),
    ("if-none-match", ""),
    ("if-range", ""),
    ("if-unmodified-since", ""),
    ("last-modified", ""),
    ("link", ""),
    ("location", ""),
    ("max-forwards", ""),
    ("proxy-authenticate", ""),
    ("proxy-authorization", ""),
    ("range", ""),
    ("referer", ""),
    ("refresh", ""),
    ("retry-after", ""),
    ("server", ""),
    ("set-cookie", ""),
    ("strict-transport-security", ""),
    ("transfer-encoding", ""),
    ("user-agent", ""),
    ("vary", ""),
    ("via", ""),
    ("www-authenticate", ""),
]

STATIC_TABLE_SIZE = len(STATIC_TABLE)

#: Static table as bytes, so every entry a decoder hands back is the same
#: type (bytes) whether it came from the static or the dynamic table.
STATIC_TABLE_BYTES = [(n.encode("ascii"), v.encode("ascii"))
                      for n, v in STATIC_TABLE]

#: Static table names only, for encoder name-index lookups: name -> lowest
#: index that has that name (RFC does not require a specific choice).
_STATIC_NAMES = {}
for _i, (_n, _v) in enumerate(STATIC_TABLE, 1):
    _STATIC_NAMES.setdefault(_n, _i)

#: Static table full (name, value) lookup for the encoder: the cheapest
#: exact-index representation. Later entries overwrite earlier ones; both
#: resolve to a valid index, so which one wins does not matter.
_STATIC_FULL = {(_n, _v): _i for _i, (_n, _v) in enumerate(STATIC_TABLE, 1)}


class HpackError(ValueError):
    """A malformed HPACK header block or an out-of-contract table size."""


# -- Huffman ----------------------------------------------------------

class _Node:
    """A node in the Huffman decoding trie. children[0] / children[1] are the
    two branches; a leaf carries the symbol it decodes to."""

    __slots__ = ("children", "symbol")

    def __init__(self):
        self.children = [None, None]
        self.symbol = -1


def _build_trie():
    root = _Node()
    for sym, (code, nbits) in enumerate(HUFFMAN_CODE):
        node = root
        for i in range(nbits):
            bit = (code >> (nbits - 1 - i)) & 1
            nxt = node.children[bit]
            if nxt is None:
                nxt = _Node()
                node.children[bit] = nxt
            node = nxt
        node.symbol = sym
    return root


_HUFFMAN_TRIE = _build_trie()


def huffman_encode(data):
    """Encode `data` (bytes) with the RFC 7541 Huffman code.

    Returns the encoded bytes. The final partial byte is padded with the most
    significant bits of the EOS code (all ones), which is what RFC 7541
    Section 5.2 requires the padding to be.
    """
    out = bytearray()
    acc = 0
    nbits = 0
    for byte in data:
        code, width = HUFFMAN_CODE[byte]
        acc = (acc << width) | code
        nbits += width
        while nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)
    if nbits:
        out.append(((acc << (8 - nbits)) | ((1 << (8 - nbits)) - 1)) & 0xFF)
    return bytes(out)


def huffman_decode(data):
    """Decode an RFC 7541 Huffman-encoded byte string.

    Raises HpackError when the stream is malformed: a code with no leaf, the
    EOS symbol inside the data, or padding that is longer than 7 bits or does
    not match the leading bits of the EOS code.
    """
    out = bytearray()
    node = _HUFFMAN_TRIE
    pending = 0  # bits consumed since the last complete symbol
    for byte in data:
        for i in range(7, -1, -1):
            bit = (byte >> i) & 1
            nxt = node.children[bit]
            if nxt is None:
                raise HpackError("invalid Huffman code in string literal")
            node = nxt
            pending += 1
            if node.symbol != -1:
                if node.symbol == 256:
                    raise HpackError("EOS symbol inside Huffman string")
                out.append(node.symbol)
                node = _HUFFMAN_TRIE
                pending = 0
    if node is not _HUFFMAN_TRIE:
        # Trailing bits are padding: they must be a prefix of the EOS code
        # (thirty 1 bits) and at most 7 bits long.
        if pending > 7:
            raise HpackError("Huffman padding longer than 7 bits")
        probe = _HUFFMAN_TRIE
        for _ in range(pending):
            probe = probe.children[1]
            if probe is None:
                raise HpackError("Huffman padding not a prefix of EOS")
        if probe is not node:
            raise HpackError("Huffman padding not a prefix of EOS")
    return bytes(out)


# -- Variable-length integers -----------------------------------------

def _encode_int(value, prefix_bits):
    """Encode `value` as an integer with a `prefix_bits`-bit prefix.

    Returns a list of octet values: the prefix octet (with the low
    `prefix_bits` bits carrying the value or the escape marker) followed by
    the continuation octets. Callers OR the prefix octet with their own
    pattern bits.
    """
    max_prefix = (1 << prefix_bits) - 1
    if value < max_prefix:
        return [value]
    out = [max_prefix]
    value -= max_prefix
    while value >= 128:
        out.append((value % 128) + 128)
        value //= 128
    out.append(value)
    return out


def _decode_int(data, pos, prefix_bits, first=None):
    """Decode an integer whose representation starts at `pos`.

    `first` optionally supplies the already-read first octet (so callers that
    peeked at it for a pattern bit do not double-read). Returns
    (value, new_pos).
    """
    if first is None:
        first = data[pos]
        pos += 1
    max_prefix = (1 << prefix_bits) - 1
    value = first & max_prefix
    if value < max_prefix:
        return value, pos
    shift = 0
    while True:
        if pos >= len(data):
            raise HpackError("truncated integer in header block")
        octet = data[pos]
        pos += 1
        value += (octet & 0x7F) << shift
        shift += 7
        if not octet & 0x80:
            break
        if shift > 63:
            raise HpackError("integer too large in header block")
    return value, pos


# -- String literals --------------------------------------------------

def _encode_string(value, huffman=True):
    """Encode a byte string literal: the H bit, a 7-bit-prefix length, and
    the data (Huffman-encoded when that is smaller)."""
    if huffman:
        encoded = huffman_encode(value)
        if len(encoded) < len(value):
            first = 0x80
            data = encoded
        else:
            first = 0x00
            data = value
    else:
        first = 0x00
        data = value
    head = _encode_int(len(data), 7)
    head[0] |= first
    return bytes(head) + data


def _decode_string(data, pos):
    """Decode a string literal at `pos`. Returns (bytes, new_pos)."""
    if pos >= len(data):
        raise HpackError("truncated string literal in header block")
    first = data[pos]
    pos += 1
    huffman = bool(first & 0x80)
    length, pos = _decode_int(data, pos, 7, first=first)
    if pos + length > len(data):
        raise HpackError("string literal overruns header block")
    raw = data[pos:pos + length]
    pos += length
    if huffman:
        raw = huffman_decode(raw)
    return raw, pos


# -- Dynamic table ----------------------------------------------------

class DynamicTable:
    """The HPACK dynamic table: a bounded FIFO of (name, value) entries.

    Index 1 is the newest entry (the front of the list), matching RFC 7541
    Section 2.3.2. `max_size` bounds the sum of entry sizes; the size of an
    entry is len(name) + len(value) + 32 (Section 4.1).
    """

    def __init__(self, max_size=4096):
        self.max_size = max_size
        self._entries = []  # newest first
        self.size = 0

    def _entry_size(self, name, value):
        return len(name) + len(value) + 32

    def _evict_to(self, target):
        while self._entries and self.size > target:
            name, value = self._entries.pop()
            self.size -= self._entry_size(name, value)

    def resize(self, new_max):
        """Change the maximum size, evicting from the tail as needed."""
        self.max_size = new_max
        self._evict_to(new_max)

    def add(self, name, value):
        """Insert a new entry at the front, evicting the oldest entries until
        it fits. An entry larger than the table empties the table."""
        size = self._entry_size(name, value)
        self._evict_to(max(0, self.max_size - size))
        if size <= self.max_size:
            self._entries.insert(0, (name, value))
            self.size += size

    def get(self, index):
        """Return (name, value) for a dynamic-table index (1-based)."""
        if 1 <= index <= len(self._entries):
            return self._entries[index - 1]
        return None

    def find_name(self, name):
        """Return the lowest index whose name matches `name`, or None."""
        for i, (n, _v) in enumerate(self._entries, 1):
            if n == name:
                return i
        return None

    def find_full(self, name, value):
        """Return the lowest index whose (name, value) matches, or None."""
        for i, entry in enumerate(self._entries, 1):
            if entry == (name, value):
                return i
        return None


def _entry_size(name, value):
    return len(name) + len(value) + 32


# -- Header block decoding --------------------------------------------

def decode_header_block(data, dynamic_table, max_table_size):
    """Decode one header block into a list of (name, value) byte pairs.

    `dynamic_table` is the connection's shared decoder-side table, mutated as
    the block instructs (incremental-indexing literals are inserted).
    `max_table_size` is the SETTINGS_HEADER_TABLE_SIZE limit the peer may not
    exceed; a dynamic table size update above it is a protocol error.
    """
    headers = []
    pos = 0
    while pos < len(data):
        first = data[pos]
        if first & 0x80:
            # Indexed header field (Section 6.1).
            index, pos = _decode_int(data, pos, 7)
            if index == 0:
                raise HpackError("indexed header field with index 0")
            entry = _table_get(index, dynamic_table)
            if entry is None:
                raise HpackError(f"header field index {index} out of range")
            headers.append(entry)
        elif first & 0x40:
            # Literal with incremental indexing (Section 6.2.1).
            index, pos = _decode_int(data, pos, 6)
            if index == 0:
                name, pos = _decode_string(data, pos)
            else:
                entry = _table_get(index, dynamic_table)
                if entry is None:
                    raise HpackError(
                        f"header field name index {index} out of range")
                name = entry[0]
            value, pos = _decode_string(data, pos)
            headers.append((name, value))
            dynamic_table.add(name, value)
        elif first & 0x20:
            # Dynamic table size update (Section 6.3). Must precede any
            # header field representation in the block.
            new_max, pos = _decode_int(data, pos, 5)
            if new_max > max_table_size:
                raise HpackError(
                    f"dynamic table size {new_max} exceeds limit "
                    f"{max_table_size}")
            dynamic_table.resize(new_max)
        else:
            # Literal without indexing (0x00) or never indexed (0x10)
            # (Sections 6.2.2 and 6.2.3). The encoding is identical.
            index, pos = _decode_int(data, pos, 4)
            if index == 0:
                name, pos = _decode_string(data, pos)
            else:
                entry = _table_get(index, dynamic_table)
                if entry is None:
                    raise HpackError(
                        f"header field name index {index} out of range")
                name = entry[0]
            value, pos = _decode_string(data, pos)
            headers.append((name, value))
    return headers


def _table_get(index, dynamic_table):
    """Resolve an index in the combined static+dynamic address space."""
    if 1 <= index <= STATIC_TABLE_SIZE:
        return STATIC_TABLE_BYTES[index - 1]
    return dynamic_table.get(index - STATIC_TABLE_SIZE)


# -- Header block encoding --------------------------------------------

def encode_header_block(headers, dynamic_table, huffman=True):
    """Encode a list of (name, value) byte pairs into one header block.

    A straightforward encoder that uses the cheapest representation it can
    find: an exact static/dynamic match becomes an indexed reference; a name
    match becomes a literal with an indexed name; otherwise the name is
    literal too. Incremental-indexing literals are inserted into the shared
    encoder-side dynamic table so repeated fields compress across requests.
    """
    out = bytearray()
    for name, value in headers:
        # Exact match in the dynamic or static table: just an index.
        index = dynamic_table.find_full(name, value)
        if index is not None:
            index += STATIC_TABLE_SIZE
        else:
            index = _STATIC_FULL.get((name, value))
        if index is not None:
            head = _encode_int(index, 7)
            head[0] |= 0x80
            out.extend(head)
            continue

        # A name match buys an indexed name; still add to the dynamic table
        # so the value compresses on the next identical field.
        name_index = dynamic_table.find_name(name)
        if name_index is None:
            name_index = _STATIC_NAMES.get(name)
        if name_index is not None:
            name_index += STATIC_TABLE_SIZE
        head = _encode_int(name_index or 0, 6)
        head[0] |= 0x40
        out.extend(head)
        if not name_index:
            out.extend(_encode_string(name, huffman))
        out.extend(_encode_string(value, huffman))
        dynamic_table.add(name, value)
    return bytes(out)