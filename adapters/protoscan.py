"""Schema-free protobuf wire-format scanner.

Antigravity stores conversation steps as protobuf blobs with no schema
available. We walk the wire format generically: read tag/value pairs, recurse
into length-delimited fields that parse cleanly as nested messages, and collect
the ones that decode as plausible UTF-8 text.

This is heuristic by construction. Everything it produces is marked
confidence="heuristic" downstream.
"""
from __future__ import annotations

MAX_DEPTH = 12
MIN_TEXT = 8


def _varint(buf, i):
    result = 0
    shift = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def _looks_texty(s: str) -> bool:
    if len(s) < MIN_TEXT:
        return False
    printable = sum(1 for c in s if c.isprintable() or c in "\n\r\t")
    if printable / len(s) < 0.9:
        return False
    # Reject blobs that are mostly punctuation/base64 noise with no whitespace.
    letters = sum(1 for c in s if c.isalpha())
    return letters / len(s) > 0.3


def _parse(buf, depth, out, path):
    """Walk one message. Returns True if the whole buffer parsed as protobuf."""
    i = 0
    n = len(buf)
    fields = 0
    while i < n:
        try:
            key, i = _varint(buf, i)
        except ValueError:
            return False
        wire = key & 7
        fnum = key >> 3
        if fnum == 0:
            return False
        if wire == 0:
            try:
                _, i = _varint(buf, i)
            except ValueError:
                return False
        elif wire == 1:
            i += 8
            if i > n:
                return False
        elif wire == 5:
            i += 4
            if i > n:
                return False
        elif wire == 2:
            try:
                ln, i = _varint(buf, i)
            except ValueError:
                return False
            if ln < 0 or i + ln > n:
                return False
            sub = buf[i:i + ln]
            i += ln
            handled = False
            if depth < MAX_DEPTH and len(sub) >= 2:
                nested = []
                if _parse(sub, depth + 1, nested, path + (fnum,)):
                    out.extend(nested)
                    handled = True
            if not handled:
                try:
                    s = sub.decode("utf-8")
                except UnicodeDecodeError:
                    s = None
                if s and _looks_texty(s):
                    out.append((path + (fnum,), s))
        else:
            return False  # groups: unsupported, treat as non-protobuf
        fields += 1
        if fields > 20000:
            return False
    return True


def extract_strings(blob):
    """Yield (field_path, text) for every plausible string in a protobuf blob."""
    if not blob:
        return []
    out = []
    try:
        _parse(bytes(blob), 0, out, ())
    except (ValueError, RecursionError):
        return out
    return out
