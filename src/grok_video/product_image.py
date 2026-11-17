"""Bounded product-image loading for multipart uploads, without provider credentials."""
import base64
import io
from ipaddress import ip_address
import socket
import time
from urllib.parse import urlsplit, urlunsplit
import warnings

import urllib3

from .client import GrokVideoClientError
from .schemas import GrokVideoTaskRequest


LIMIT = 10 * 1024 * 1024


def _download(url):
    parsed = urlsplit(url)
    host = parsed.hostname
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError
        # Pin the validated IP so a second DNS lookup cannot reach a private host.
        options = dict(host=addresses[0][4][0], port=port,
                       timeout=urllib3.Timeout(connect=10, read=20), retries=False)
        if parsed.scheme == "https":
            pool = urllib3.HTTPSConnectionPool(**options, assert_hostname=host, server_hostname=host)
        else:
            pool = urllib3.HTTPConnectionPool(**options)
        with pool:
            response = pool.urlopen(
                "GET", urlunsplit(("", "", parsed.path or "/", parsed.query, "")),
                headers={"Host": parsed.netloc, "Accept-Encoding": "identity"},
                redirect=False, preload_content=False,
            )
            try:
                if response.status != 200:
                    raise ValueError
                length = response.headers.get("Content-Length")
                if length and (int(length) < 0 or int(length) > LIMIT):
                    raise ValueError
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError
                data = bytearray()
                deadline = time.monotonic() + 60
                while chunk := response.read(64 * 1024, decode_content=False):
                    data.extend(chunk)
                    if len(data) > LIMIT or time.monotonic() > deadline:
                        raise ValueError
                if length and len(data) != int(length):
                    raise ValueError
                return bytes(data)
            finally:
                response.close()
    except (OSError, ValueError, urllib3.exceptions.HTTPError):
        raise GrokVideoClientError(
            "Product image download failed; use a direct public image URL without redirects, at most 10 MiB"
        ) from None


def load(url):
    from PIL import Image, UnidentifiedImageError

    try:
        GrokVideoTaskRequest.validate_image_url(url)
        raw = base64.b64decode(url.split(",", 1)[1], validate=True) if url.startswith("data:") else _download(url)
        if not raw or len(raw) > LIMIT:
            raise ValueError
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                formats = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"),
                           "WEBP": ("webp", "image/webp")}
                extension, mime = formats[image.format]
                if image.width * image.height > 25_000_000 or getattr(image, "n_frames", 1) != 1:
                    raise ValueError
                image.verify()
        return (f"product.{extension}", raw, mime)
    except (ValueError, KeyError, OSError, UnidentifiedImageError,
            Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise GrokVideoClientError("Product image must be a valid PNG/JPEG/WebP up to 10 MiB and 25 MP") from None
