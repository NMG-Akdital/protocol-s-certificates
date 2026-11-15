"""Real QR codes, encoded from each certificate's actual verification URL."""
from __future__ import annotations

import base64
import io

import segno


def qr_svg_data_uri(url: str, error: str = "q") -> str:
    """Vector QR (sharp at any print size). Error correction Q (~25%) so it
    still scans if the printed sheet is creased or slightly marked."""
    qr = segno.make(url, error=error, micro=False)
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=10, border=0, dark="#0E2240", light=None, xmldecl=False, svgns=True)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def qr_png_bytes(url: str, scale: int = 10) -> bytes:
    qr = segno.make(url, error="q", micro=False)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=scale, border=4, dark="#0E2240")
    return buf.getvalue()
