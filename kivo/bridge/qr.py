"""QR code generation and in-memory PNG rendering for Phone Bridge."""
from __future__ import annotations

import base64
import io
from urllib.parse import urlencode

import qrcode
from PIL import Image

from kivo.bridge.models import PAIRING_PROTOCOL_VERSION, PairingOffer


def build_qr_uri(
    lan_ip: str,
    port: int,
    cert_fp: str,
    pairing_secret_b64: str,
    session_id: str,
    server_nonce_b64: str,
    expires_at: int,
) -> str:
    """Construct standard kivo-pair:// URI encoded in the QR code."""
    query = urlencode({
        "v": PAIRING_PROTOCOL_VERSION,
        "addr": f"{lan_ip}:{port}",
        "fp": cert_fp.upper(),
        "t": pairing_secret_b64,
        "id": session_id,
        "sn": server_nonce_b64,
        "exp": str(expires_at),
    })
    return f"kivo-pair://v1?{query}"


def render_qr_data_url(payload: str) -> str:
    """Render a QR code into an in-memory PNG data URL without writing to disk."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    b64_png = base64.b64encode(buffer.getvalue()).decode("ascii")

    return f"data:image/png;base64,{b64_png}"


__all__ = ["build_qr_uri", "render_qr_data_url"]
