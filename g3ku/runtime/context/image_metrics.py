"""Image dimension readers that need no decoder.

The send-preflight and frontdoor lanes both price an inline image by its tile count, so a
format they cannot read falls back to the 512x512 default and an over-window request leaves
the box unchecked. WebP and BMP are accepted by the media surface, so their headers are
parsed here - the one owner both lanes call.
"""

from __future__ import annotations

_WEBP_CHUNK_DATA_OFFSET = 20
_VP8L_SIGNATURE = 0x2F
_VP8_SYNC_BYTES = b"\x9d\x01\x2a"
_DIMENSION_MASK_14_BITS = 0x3FFF


def webp_dimensions(data: bytes) -> tuple[int, int] | None:
    """Canvas size from a WebP header, for all three container flavours.

    ``VP8X`` stores width/height minus one as 24-bit little-endian, ``VP8L`` packs both as
    14-bit fields behind a signature byte, and lossy ``VP8 `` carries them after the sync
    bytes. Anything else (a colour-profile-only file, a truncated header) returns None and
    the caller keeps its own default.
    """
    if len(data) < _WEBP_CHUNK_DATA_OFFSET or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    four_cc = data[12:16]
    payload = data[_WEBP_CHUNK_DATA_OFFSET:]
    if four_cc == b"VP8L":
        if len(payload) < 5 or payload[0] != _VP8L_SIGNATURE:
            return None
        packed = int.from_bytes(payload[1:5], "little")
        width = (packed & _DIMENSION_MASK_14_BITS) + 1
        height = ((packed >> 14) & _DIMENSION_MASK_14_BITS) + 1
        return width, height
    if four_cc == b"VP8X":
        if len(payload) < 10:
            return None
        width = int.from_bytes(payload[4:7], "little") + 1
        height = int.from_bytes(payload[7:10], "little") + 1
        return width, height
    if four_cc == b"VP8 ":
        if len(payload) < 10 or payload[3:6] != _VP8_SYNC_BYTES:
            return None
        width = int.from_bytes(payload[6:8], "little") & _DIMENSION_MASK_14_BITS
        height = int.from_bytes(payload[8:10], "little") & _DIMENSION_MASK_14_BITS
        return (width, height) if width > 0 and height > 0 else None
    return None


def bmp_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width/height from a BMP DIB header; a negative height is a top-down bitmap."""
    if len(data) < 26 or data[:2] != b"BM":
        return None
    width = int.from_bytes(data[18:22], "little", signed=True)
    height = abs(int.from_bytes(data[22:26], "little", signed=True))
    return (width, height) if width > 0 and height > 0 else None


__all__ = ["bmp_dimensions", "webp_dimensions"]
