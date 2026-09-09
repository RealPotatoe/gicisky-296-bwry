"""
BLE driver for the Gicisky 296x128 BWRY e-paper tag (device type 0x002E).

This module implements only the vendor GATT protocol required to push a
pre-encoded framebuffer to the tag. Image encoding lives in ``encoder.py``.
"""

import asyncio
import logging
import math
from functools import partial
from typing import Self

from bleak import BleakClient
from bleak.exc import BleakError
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

# The negotiated block size includes the four-byte block index.
BLOCK_HEADER_SIZE = 4
# write-with-response is limited to 512 bytes, independent of the MTU.
MAX_BLOCK_SIZE = 512

CONNECTION_TIMEOUT = 30
NOTIFICATION_TIMEOUT = 10
TRANSFER_TIMEOUT = 300
MAX_RETRIES = 3


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

    def __init__(
        self,
        address: str,
        *,
        connection_timeout: float = CONNECTION_TIMEOUT,
        notification_timeout: float = NOTIFICATION_TIMEOUT,
        transfer_timeout: float = TRANSFER_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ):
        for name, value in (
            ("connection_timeout", connection_timeout),
            ("notification_timeout", notification_timeout),
            ("transfer_timeout", transfer_timeout),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise TypeError("max_retries must be an integer")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")

        self.address = address
        self._connection_timeout = connection_timeout
        self._notification_timeout = notification_timeout
        self._transfer_timeout = transfer_timeout
        self._max_retries = max_retries
        self._client: BleakClient | None = None
        self._block_size: int | None = None
        self._operation_lock = asyncio.Lock()
        self._notification = asyncio.Event()
        self._notification_data: bytes | None = None

    # -- connection lifecycle -------------------------------------------------

    async def connect(self) -> None:
        """Connect and subscribe to replies; clean up if setup fails."""
        async with self._operation_lock:
            if self._client is not None:
                if self._client.is_connected:
                    return
                await self._disconnect()

            logger.info("Connecting to %s...", self.address)
            ready = False
            try:
                client = BleakClient(
                    self.address,
                    disconnected_callback=self._disconnected_handler,
                    timeout=self._connection_timeout,
                )
                self._client = client
                async with asyncio.timeout(self._connection_timeout):
                    await client.connect()
                    self._require_client()
                    await client.start_notify(
                        CMD_UUID, partial(self._notification_handler, client)
                    )
                    # Give BlueZ/device a moment to establish notifications.
                    await asyncio.sleep(0.5)
                    self._require_client()
                ready = True
            except (BleakError, OSError) as exc:
                raise GiciskyConnectionError(
                    f"Could not connect to {self.address}: {exc!r}"
                ) from exc
            finally:
                if not ready:
                    await self._disconnect(suppress_errors=True)

            logger.info("Connected to %s", self.address)

    async def disconnect(self) -> None:
        """Disconnect after any active operation and clear local state."""
        async with self._operation_lock:
            await self._disconnect()

    async def _disconnect(self, *, suppress_errors: bool = False) -> None:
        client = self._client
        self._client = None
        self._block_size = None
        self._notification_data = None
        self._notification.clear()
        if client is None:
            return

        try:
            async with asyncio.timeout(self._connection_timeout):
                # Bleak stops notifications automatically on disconnect.
                await client.disconnect()
        except (BleakError, OSError) as exc:
            if not suppress_errors:
                raise GiciskyConnectionError(
                    f"Could not disconnect from {self.address}: {exc!r}"
                ) from exc
            logger.warning("Disconnect failed during error cleanup", exc_info=True)

    def _require_client(self) -> BleakClient:
        if self._client is None or not self._client.is_connected:
            raise GiciskyConnectionError("Not connected; call connect() first")
        return self._client

    def _disconnected_handler(self, client: BleakClient) -> None:
        if client is not self._client:
            return
        self._block_size = None
        self._notification.set()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            await self.disconnect()
        except GiciskyConnectionError:
            if exc is None:
                raise
            logger.warning("Disconnect failed during context cleanup", exc_info=True)

    # -- public API -------------------------------------------------------

    async def display(self, image: Image.Image) -> None:
        """Encode ``image`` and upload it to the tag."""
        framebuffer = encode(image)
        await self.upload(framebuffer)

    async def upload(self, framebuffer: bytes | bytearray | memoryview) -> None:
        """Upload exactly one framebuffer; disconnect on failure or cancellation."""
        if not isinstance(framebuffer, (bytes, bytearray, memoryview)):
            raise TypeError("framebuffer must be bytes, bytearray, or memoryview")
        # Snapshot mutable buffers before waiting for another upload to finish.
        framebuffer = bytes(framebuffer)
        expected = WIDTH * HEIGHT // 4
        if len(framebuffer) != expected:
            raise ValueError(
                f"Expected {expected} framebuffer bytes, got {len(framebuffer)}"
            )

        async with self._operation_lock:
            self._require_client()
            completed = False
            try:
                async with asyncio.timeout(self._transfer_timeout):
                    block_size = await self._request_block_size()
                    await self._request_write_screen(framebuffer)
                    await self._transfer(framebuffer, block_size)
                completed = True
            except TimeoutError as exc:
                raise GiciskyTransferError(
                    f"Image transfer timed out after {self._transfer_timeout}s"
                ) from exc
            finally:
                if not completed:
                    await self._disconnect(suppress_errors=True)

            logger.info("Image transfer completed.")

    # -- notification plumbing --------------------------------------------

    def _notification_handler(
        self, client: BleakClient, _sender, data: bytearray
    ) -> None:
        if client is self._client and not self._notification.is_set():
            self._notification_data = bytes(data)
            self._notification.set()

    async def _exchange(self, uuid: str, packet: bytes) -> bytes:
        client = self._require_client()
        self._notification.clear()
        self._notification_data = None
        try:
            # Bound the write as well as the notification wait.
            async with asyncio.timeout(self._notification_timeout):
                await client.write_gatt_char(uuid, packet, response=True)
                await self._notification.wait()
            data = self._notification_data
            if data is None:
                raise GiciskyConnectionError(f"Disconnected from {self.address}")
            logger.debug("RX: %s", data.hex())
            return data
        except TimeoutError as exc:
            raise GiciskyProtocolError(
                f"Timed out exchanging data with {self.address} on {uuid}"
            ) from exc
        except (BleakError, OSError) as exc:
            raise GiciskyConnectionError(
                f"BLE write to {self.address} on {uuid} failed: {exc}"
            ) from exc

    async def _command(self, packet: bytes) -> bytes:
        logger.debug("CMD: %s", packet.hex())
        return await self._exchange(CMD_UUID, packet)

    # -- protocol steps -----------------------------------------------------

    async def _request_block_size(self) -> int:
        data = await self._command(bytes([0x01]))

        if len(data) != 3 or data[0] != 0x01:
            raise GiciskyProtocolError(f"Unexpected block-size response: {data.hex()}")

        block_size = int.from_bytes(data[1:3], "little")

        logger.info("Tag block size: %d bytes", block_size)

        if not 8 <= block_size <= MAX_BLOCK_SIZE:
            raise GiciskyProtocolError(
                f"Invalid block size {block_size}; expected 8..{MAX_BLOCK_SIZE}"
            )

        self._block_size = block_size
        return block_size

    async def _request_write_screen(self, framebuffer: bytes) -> None:
        # The vendor protocol has an obsolete image-type field.
        # hass-gicisky sends three zero bytes after the 4-byte size.
        packet = (
            bytes([0x02])
            + len(framebuffer).to_bytes(4, "little")
            + bytes([0x00, 0x00, 0x00])
        )

        data = await self._command(packet)

        if len(data) < 2 or data[0] != 0x02:
            raise GiciskyProtocolError(
                f"Unexpected screen-write response: {data.hex()}"
            )
        if data[1] != 0x00:
            raise GiciskyTransferError(f"Screen-write rejected: {data.hex()}")

    async def _request_start_transfer(self) -> int:
        data = await self._command(bytes([0x03]))

        if len(data) < 2 or data[0] != 0x05:
            raise GiciskyProtocolError(
                f"Unexpected transfer-start response: {data.hex()}"
            )
        if data[1] != 0x00:
            raise GiciskyTransferError(f"Transfer start rejected: {data.hex()}")
        if len(data) < 6:
            raise GiciskyProtocolError(
                f"Truncated transfer-start response: {data.hex()}"
            )

        return int.from_bytes(data[2:6], "little")

    async def _send_block(
        self, framebuffer: bytes, block_size: int, part: int
    ) -> int | None:
        payload_size = block_size - BLOCK_HEADER_SIZE

        start = part * payload_size
        if part < 0 or start >= len(framebuffer):
            raise GiciskyTransferError(f"Invalid block index {part}")
        chunk = framebuffer[start : start + payload_size]

        packet = part.to_bytes(4, "little") + chunk

        logger.debug("TX block %d (%d/%d bytes)", part, len(chunk), len(framebuffer))

        data = await self._exchange(IMG_UUID, packet)

        if len(data) < 2 or data[0] != 0x05:
            raise GiciskyProtocolError(f"Unexpected block response: {data.hex()}")

        if data[1] != 0x00:
            # 0x08 means the tag considers the transfer complete.
            if data[1] == 0x08:
                return None
            raise GiciskyTransferError(f"Block {part} rejected: {data.hex()}")

        if len(data) < 6:
            raise GiciskyProtocolError(f"Truncated block response: {data.hex()}")
        return int.from_bytes(data[2:6], "little")

    async def _transfer(self, framebuffer: bytes, block_size: int) -> None:
        part = await self._request_start_transfer()
        payload_size = block_size - BLOCK_HEADER_SIZE
        block_count = (len(framebuffer) + payload_size - 1) // payload_size
        if part >= block_count:
            raise GiciskyProtocolError(f"Invalid starting block {part}")

        # A valid initial offset can resume a transfer. Subsequent requests may
        # retransmit earlier blocks, but must not skip any remaining image data.
        next_unsent = part
        attempts = [0] * block_count

        while True:
            attempts[part] += 1
            if attempts[part] > self._max_retries + 1:
                raise GiciskyTransferError(f"Retry limit exceeded for block {part}")

            next_part = await self._send_block(framebuffer, block_size, part)
            next_unsent = max(next_unsent, part + 1)

            if next_part is None or next_part == block_count:
                if next_unsent != block_count:
                    raise GiciskyProtocolError(
                        f"Tag reported completion before block {next_unsent} was sent"
                    )
                return

            if next_part >= block_count or next_part > next_unsent:
                raise GiciskyProtocolError(
                    f"Invalid next block {next_part}; next unsent block is {next_unsent}"
                )
            part = next_part
