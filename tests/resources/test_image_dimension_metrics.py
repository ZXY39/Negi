"""Image dimension readers behind the send-preflight tile estimate.

webp/bmp are accepted by the media surface but were never parsed, so an inline image in one
of those formats was priced as the 512x512 default: the tile count, the token estimate and
the compression trigger all read a size the file never had.
"""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from g3ku.runtime.context.image_metrics import bmp_dimensions, webp_dimensions


def _encode_webp(size: tuple[int, int], **save_kwargs: object) -> bytes:
    image = Image.new("RGBA" if save_kwargs else "RGB", size, (12, 34, 56, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", **save_kwargs)  # type: ignore[arg-type]
    return buffer.getvalue()


def _encode_bmp(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 30, 90)).save(buffer, format="BMP")
    return buffer.getvalue()


def _data_url(mime: str, data: bytes) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


@pytest.mark.parametrize(
    ("encoder", "flavour"),
    [
        (lambda size: _encode_webp(size), "VP8 lossy"),
        (lambda size: _encode_webp(size, lossless=True), "VP8L lossless"),
        (lambda size: _encode_webp(size, icc_profile=b"\x00\x01\x02"), "VP8X extended"),
        (_encode_bmp, "BMP"),
    ],
    ids=["vp8-lossy", "vp8l-lossless", "vp8x-extended", "bmp"],
)
def test_header_parsing_matches_the_decoder_oracle(encoder, flavour) -> None:
    """四种真实编码都按表头读出 PIL 解出来的同一尺寸。"""
    expected = (1000, 640)
    data = encoder(expected)
    parsed = bmp_dimensions(data) if flavour == "BMP" else webp_dimensions(data)

    assert parsed == expected, f"{flavour}: header said {parsed}"
    assert Image.open(io.BytesIO(data)).size == expected


@pytest.mark.parametrize(
    "corrupt",
    [b"", b"RIFFxxxxWEBP", b"RIFF\x00\x00\x00\x00WEBPVP8 \x00\x00\x00", b"BM" + bytes(20)],
    ids=["empty", "no-chunk", "truncated-lossy", "short-bmp"],
)
def test_unreadable_headers_fall_back_to_the_caller_default(corrupt: bytes) -> None:
    assert webp_dimensions(corrupt) is None
    assert bmp_dimensions(corrupt) is None


def test_send_preflight_prices_a_webp_by_its_real_size() -> None:
    from main.runtime import send_token_preflight as preflight

    payload = preflight._image_block_payload(
        _data_url("image/webp", _encode_webp((1000, 640))), detail=None
    )

    assert (payload["width"], payload["height"]) == (1000, 640)
    # 512×512 默认只要 1 块（70+140=210）；真实尺寸是 2×2 块。
    assert payload["estimated_tokens"] == 70 + 4 * 140


def test_send_preflight_prices_a_bmp_by_its_real_size() -> None:
    from main.runtime import send_token_preflight as preflight

    payload = preflight._image_block_payload(_data_url("image/bmp", _encode_bmp((1600, 100))), detail=None)

    assert (payload["width"], payload["height"]) == (1600, 100)
    assert payload["estimated_tokens"] == 70 + 4 * 140


def test_frontdoor_reports_the_same_size_without_a_declared_mime() -> None:
    """免 MIME 声明的那半条道也要认：前门按魔术字节兜底。"""
    from g3ku.runtime.frontdoor import _ceo_runtime_ops as frontdoor

    data = _encode_webp((1000, 640))

    assert frontdoor._image_dimensions_from_bytes(data, mime_type="") == (1000, 640)
    assert frontdoor._image_dimensions_from_bytes(_encode_bmp((1600, 100)), mime_type="") == (1600, 100)

    payload = frontdoor._image_block_payload(_data_url("image/webp", data), detail=None)
    assert (payload["width"], payload["height"]) == (1000, 640)
    assert payload["estimated_tokens"] == 70 + 4 * 140
