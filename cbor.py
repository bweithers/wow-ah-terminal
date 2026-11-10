"""Minimal CBOR decoder (RFC 8949), dependency-free.

Enough of the spec to read the packed price database: unsigned/negative
integers, byte and text strings (definite and indefinite length), arrays,
maps, tags, floats, and the simple values true/false/null/undefined.
"""

from __future__ import annotations

import struct

__all__ = ["loads", "CborError"]


class CborError(Exception):
    pass


_BREAK = object()


class _Decoder:
    def __init__(self, data: bytes):
        self.d = data
        self.i = 0

    def u8(self) -> int:
        if self.i >= len(self.d):
            raise CborError("truncated input")
        b = self.d[self.i]
        self.i += 1
        return b

    def take(self, n: int) -> bytes:
        if self.i + n > len(self.d):
            raise CborError(f"truncated: want {n} bytes at {self.i}")
        out = self.d[self.i : self.i + n]
        self.i += n
        return out

    def length(self, ai: int):
        """Resolve a head's additional-info field to a length/value."""
        if ai < 24:
            return ai
        if ai == 24:
            return self.u8()
        if ai == 25:
            return struct.unpack(">H", self.take(2))[0]
        if ai == 26:
            return struct.unpack(">I", self.take(4))[0]
        if ai == 27:
            return struct.unpack(">Q", self.take(8))[0]
        if ai == 31:
            return None  # indefinite length
        raise CborError(f"reserved additional info {ai}")

    def value(self):
        head = self.u8()
        major, ai = head >> 5, head & 0x1F

        if major == 0:
            return self.length(ai)
        if major == 1:
            return -1 - self.length(ai)

        if major in (2, 3):
            n = self.length(ai)
            if n is None:
                chunks = []
                while True:
                    v = self.value()
                    if v is _BREAK:
                        break
                    chunks.append(v if isinstance(v, bytes) else v.encode())
                raw = b"".join(chunks)
            else:
                raw = self.take(n)
            return raw if major == 2 else raw.decode("utf-8", "replace")

        if major == 4:
            n = self.length(ai)
            out = []
            if n is None:
                while True:
                    v = self.value()
                    if v is _BREAK:
                        break
                    out.append(v)
            else:
                for _ in range(n):
                    out.append(self.value())
            return out

        if major == 5:
            n = self.length(ai)
            out = {}
            if n is None:
                while True:
                    k = self.value()
                    if k is _BREAK:
                        break
                    out[_key(k)] = self.value()
            else:
                for _ in range(n):
                    k = _key(self.value())
                    out[k] = self.value()
            return out

        if major == 6:
            self.length(ai)  # tag number, ignored
            return self.value()

        # major 7: simple values and floats
        if ai == 20:
            return False
        if ai == 21:
            return True
        if ai == 22:
            return None
        if ai == 23:
            return None
        if ai == 25:
            return struct.unpack(">e", self.take(2))[0]
        if ai == 26:
            return struct.unpack(">f", self.take(4))[0]
        if ai == 27:
            return struct.unpack(">d", self.take(8))[0]
        if ai == 31:
            return _BREAK
        if ai == 24:
            return self.u8()
        return ai


def _key(k):
    """Make a decoded value usable as a dict key."""
    if isinstance(k, bytes):
        return k.decode("utf-8", "replace")
    if isinstance(k, (list, dict)):
        return repr(k)
    return k


def loads(data: bytes, allow_trailing: bool = False):
    """Decode one CBOR item. Raises unless the whole buffer is consumed."""
    dec = _Decoder(data)
    val = dec.value()
    if not allow_trailing and dec.i != len(data):
        raise CborError(f"{len(data) - dec.i} trailing bytes after item")
    return val
