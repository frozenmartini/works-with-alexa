"""The example landing page shown on the browser sign-in step."""

from __future__ import annotations

import base64
from functools import cache
from pathlib import Path

LANDING_IMAGE = Path(__file__).with_name("signin_landing_page.jpg")


@cache
def landing_image_uri() -> str:
    """Return Amazon's landing page example as a data URI, reading the file once.

    A data URI needs no static route and no http dependency; the frontend's
    markdown sanitizer allows data:image sources in flow descriptions.
    """
    return "data:image/jpeg;base64," + base64.b64encode(LANDING_IMAGE.read_bytes()).decode()
