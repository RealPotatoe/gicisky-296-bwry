"""
BLE driver for the Gicisky 296x128 BWRY e-paper tag (device type 0x002E).

This module implements only the vendor GATT protocol required to push a
pre-encoded framebuffer to the tag. Image encoding lives in ``encoder.py``.
"""

import asyncio
import logging
from typing import Self

from bleak import BleakClient
from PIL import Image

from .encoder import HEIGHT, WIDTH, encode
from .exceptions import (
    GiciskyConnectionError,
    GiciskyProtocolError,
    GiciskyTransferError,
)

logger = logging.getLogger(__name__)

CMD_UUID = "0000fef1-0000-1000-8000-00805f9b34fb"
IMG_UUID = "0000fef2-0000-1000-8000-00805f9b34fb"

# Block index (4 bytes) + payload. Actual block size is negotiated with
# the tag via the 0x01 command; this is only a sane fallback bound.
BLOCK_HEADER_SIZE = 4

NOTIFICATION_TIMEOUT = 10


class GiciskyTag:
    """
    Driver for a single Gicisky 296x128 BWRY tag.

    Usage::

        async with GiciskyTag("FF:FF:92:83:37:21") as tag:
            await tag.display(image)
    """

    WIDTH = WIDTH
    HEIGHT = HEIGHT
    COLORS = ("black", "white", "yellow", "red")

    def __init__(self, address: str):
        self.address = address
        self._client: BleakClient | None = None
        self._block_size: int | None = None
        self._notification = asyncio.Event()
        self._notification_data: bytes | None = None

    # -- connection lifecycle -------------------------------------------------

    async def connect(self):
        if self._client is not None:
            return

        logger.info("Connecting to %s...", self.address)

        client = BleakClient(self.address)

        try:
            await client.connect()
        except Exception as exc:
            raise GiciskyConnectionError(
                f"Could not connect to {self.address}: {exc}"
            ) from exc

        logger.info("Connected: %s", client.is_connected)

        self._client = client

        await client.start_notify(CMD_UUID, self._notification_handler)

        # Give BlueZ/device a moment to establish notifications.
        await asyncio.sleep(0.5)

    async def disconnect(self):
        if self._client is None:
            return

        try:
            await self._client.stop_notify(CMD_UUID)
        finally:
            await self._client.disconnect()
            self._client = None
            self._block_size = None

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.disconnect()

    # -- public API -------------------------------------------------------

    async def display(self, image: Image.Image):
        """Encode ``image`` and upload it to the tag."""
        framebuffer = encode(image)
        await self.upload(framebuffer)

    async def upload(self, framebuffer: bytes):
        """Upload a pre-encoded framebuffer to the tag."""
        if self._client is None:
            raise GiciskyConnectionError("Not connected; call connect() first")

        block_size = await self._request_block_size()
        await self._request_write_screen(framebuffer)
        await self._transfer(framebuffer, block_size)

        logger.info("SUCCESS: image transfer completed.")

    # -- notification plumbing --------------------------------------------

    def _notification_handler(self, _sender, data):
        self._notification_data = bytes(data)
        self._notification.set()

    async def _wait_notification(self) -> bytes:
        try:
            await asyncio.wait_for(self._notification.wait(), NOTIFICATION_TIMEOUT)
        except TimeoutError as exc:
            raise GiciskyProtocolError("Timed out waiting for tag response") from exc

        data = self._notification_data
        logger.debug("RX: %s", data.hex())
        return data

    async def _command(self, packet: bytes) -> bytes:
        self._notification.clear()
        self._notification_data = None

        logger.debug("CMD: %s", packet.hex())

        await self._client.write_gatt_char(CMD_UUID, packet, response=True)

        return await self._wait_notification()

    # -- protocol steps -----------------------------------------------------

    async def _request_block_size(self) -> int:
        data = await self._command(bytes([0x01]))

        if len(data) != 3 or data[0] != 0x01:
            raise GiciskyProtocolError(f"Unexpected block-size response: {data.hex()}")

        block_size = int.from_bytes(data[1:3], "little")

        logger.info("Tag block size: %d bytes", block_size)

        if block_size < 8:
            raise GiciskyProtocolError("Invalid block size")

        self._block_size = block_size
        return block_size

    async def _request_write_screen(self, framebuffer: bytes):
        # The vendor protocol has an obsolete image-type field.
        # hass-gicisky sends three zero bytes after the 4-byte size.
        packet = (
            bytes([0x02])
            + len(framebuffer).to_bytes(4, "little")
            + bytes([0x00, 0x00, 0x00])
        )

        data = await self._command(packet)

        if len(data) < 2 or data[0] != 0x02 or data[1] != 0x00:
            raise GiciskyProtocolError(f"Screen-write rejected: {data.hex()}")

    async def _request_start_transfer(self) -> int:
        data = await self._command(bytes([0x03]))

        if len(data) < 6 or data[0] != 0x05 or data[1] != 0x00:
            raise GiciskyProtocolError(f"Transfer start rejected: {data.hex()}")

        return int.from_bytes(data[2:6], "little")

    async def _send_block(self, framebuffer: bytes, block_size: int, part: int):
        payload_size = block_size - BLOCK_HEADER_SIZE

        start = part * payload_size
        chunk = framebuffer[start : start + payload_size]

        if not chunk:
            raise GiciskyTransferError(f"Empty block {part}")

        packet = part.to_bytes(4, "little") + chunk

        logger.debug("TX block %d (%d/%d bytes)", part, len(chunk), len(framebuffer))

        self._notification.clear()
        self._notification_data = None

        await self._client.write_gatt_char(IMG_UUID, packet, response=True)

        data = await self._wait_notification()

        if len(data) < 2 or data[0] != 0x05:
            raise GiciskyProtocolError(f"Unexpected block response: {data.hex()}")

        if data[1] != 0x00:
            # 0x08 means the tag considers the transfer complete.
            if data[1] == 0x08:
                return None
            raise GiciskyTransferError(f"Block {part} rejected: {data.hex()}")

        return int.from_bytes(data[2:6], "little")

    async def _transfer(self, framebuffer: bytes, block_size: int):
        part = await self._request_start_transfer()
        payload_size = block_size - BLOCK_HEADER_SIZE

        while True:
            if part * payload_size >= len(framebuffer):
                logger.info("All image data transferred.")
                return

            next_part = await self._send_block(framebuffer, block_size, part)

            if next_part is None:
                logger.info("Tag reports transfer complete.")
                return

            part = next_part
