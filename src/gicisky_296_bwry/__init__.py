"""
gicisky_296_bwry: BLE driver for the Gicisky/PICKSMART 2.9" 296x128 BWRY
e-paper display (device type 0x002E).

Typical usage::

    from PIL import Image
    from gicisky_296_bwry import GiciskyTag

    async def main():
        image = Image.open("status.png")

        async with GiciskyTag("FF:FF:92:83:37:21") as tag:
            await tag.display(image)
"""

from .device import GiciskyTag
from .encoder import HEIGHT, WIDTH, encode
from .exceptions import (
    GiciskyConnectionError,
    GiciskyError,
    GiciskyProtocolError,
    GiciskyTransferError,
)

__version__ = "0.1.0"

__all__ = [
    "HEIGHT",
    "WIDTH",
    "GiciskyConnectionError",
    "GiciskyError",
    "GiciskyProtocolError",
    "GiciskyTag",
    "GiciskyTransferError",
    "encode",
]
