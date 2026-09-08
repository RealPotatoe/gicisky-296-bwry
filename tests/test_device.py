"""
Protocol tests for gicisky_296_bwry.device.GiciskyTag.

These tests use a fake in-process BLE client and drive GiciskyTag's
protocol methods directly, without connecting to real hardware or the
real bleak/BlueZ stack.
"""

import asyncio

import pytest

from gicisky_296_bwry.device import CMD_UUID, IMG_UUID, GiciskyTag
from gicisky_296_bwry.exceptions import (
    GiciskyConnectionError,
    GiciskyProtocolError,
    GiciskyTransferError,
)


class FakeBleakClient:
    """
    Minimal stand-in for bleak.BleakClient.

    Each call to write_gatt_char() pops the next queued response and
    feeds it into the tag's notification handler, mimicking a
    synchronous request/response exchange over BLE notifications.
    """

    def __init__(self, tag: GiciskyTag, responses):
        self.tag = tag
        self.responses = list(responses)
        self.calls = []

    async def write_gatt_char(self, uuid, packet, response=True):
        self.calls.append((uuid, bytes(packet)))
        data = self.responses.pop(0)
        self.tag._notification_handler(None, data)


def make_tag(responses) -> GiciskyTag:
    tag = GiciskyTag("00:00:00:00:00:00")
    tag._client = FakeBleakClient(tag, responses)
    return tag


def block_size_response(size: int) -> bytes:
    return bytes([0x01]) + size.to_bytes(2, "little")


def screen_write_ack() -> bytes:
    return bytes([0x02, 0x00])


def start_transfer_response(part: int) -> bytes:
    return bytes([0x05, 0x00]) + part.to_bytes(4, "little")


def block_ack(next_part: int) -> bytes:
    return bytes([0x05, 0x00]) + next_part.to_bytes(4, "little")


def block_done() -> bytes:
    return bytes([0x05, 0x08])


# -- individual protocol steps --------------------------------------------


def test_request_block_size_parses_response():
    tag = make_tag([block_size_response(244)])

    block_size = asyncio.run(tag._request_block_size())

    assert block_size == 244
    assert tag._block_size == 244
    assert tag._client.calls == [(CMD_UUID, bytes([0x01]))]


def test_request_block_size_rejects_malformed_response():
    tag = make_tag([bytes([0x99, 0x00, 0x00])])

    with pytest.raises(GiciskyProtocolError):
        asyncio.run(tag._request_block_size())


def test_request_block_size_rejects_too_small_block_size():
    tag = make_tag([block_size_response(4)])

    with pytest.raises(GiciskyProtocolError):
        asyncio.run(tag._request_block_size())


def test_request_write_screen_accepts_ack():
    tag = make_tag([screen_write_ack()])

    asyncio.run(tag._request_write_screen(b"\x00" * 100))

    uuid, packet = tag._client.calls[0]
    assert uuid == CMD_UUID
    assert packet[0] == 0x02
    assert int.from_bytes(packet[1:5], "little") == 100


def test_request_write_screen_rejects_error_response():
    tag = make_tag([bytes([0x02, 0x01])])

    with pytest.raises(GiciskyProtocolError):
        asyncio.run(tag._request_write_screen(b"\x00" * 100))


def test_request_start_transfer_parses_initial_part():
    tag = make_tag([start_transfer_response(0)])

    part = asyncio.run(tag._request_start_transfer())

    assert part == 0


def test_request_start_transfer_rejects_bad_response():
    tag = make_tag([bytes([0x00, 0x00])])

    with pytest.raises(GiciskyProtocolError):
        asyncio.run(tag._request_start_transfer())


def test_send_block_returns_next_part():
    tag = make_tag([block_ack(1)])
    tag._block_size = 244

    framebuffer = b"\xab" * 240 * 3
    next_part = asyncio.run(tag._send_block(framebuffer, 244, 0))

    assert next_part == 1
    uuid, packet = tag._client.calls[0]
    assert uuid == IMG_UUID
    assert int.from_bytes(packet[0:4], "little") == 0
    assert packet[4:] == framebuffer[0:240]


def test_send_block_reports_completion():
    tag = make_tag([block_done()])

    framebuffer = b"\xab" * 20
    next_part = asyncio.run(tag._send_block(framebuffer, 244, 0))

    assert next_part is None


def test_send_block_raises_on_rejection():
    tag = make_tag([bytes([0x05, 0x01])])

    framebuffer = b"\xab" * 240
    with pytest.raises(GiciskyTransferError):
        asyncio.run(tag._send_block(framebuffer, 244, 0))


def test_send_block_raises_on_out_of_range_part():
    tag = make_tag([])

    framebuffer = b"\xab" * 10
    with pytest.raises(GiciskyTransferError):
        asyncio.run(tag._send_block(framebuffer, 244, 5))


# -- full transfer loop -----------------------------------------------------


def test_transfer_completes_via_done_signal():
    block_size = 244
    payload_size = block_size - 4  # 240
    framebuffer = b"\xcd" * (payload_size * 2 + 20)  # 3 blocks, last partial

    tag = make_tag(
        [
            start_transfer_response(0),
            block_ack(1),
            block_ack(2),
            block_done(),
        ]
    )
    tag._block_size = block_size

    asyncio.run(tag._transfer(framebuffer, block_size))

    # start + 3 blocks = 4 writes total.
    assert len(tag._client.calls) == 4


def test_transfer_completes_via_size_check():
    block_size = 244
    payload_size = block_size - 4  # 240
    framebuffer = b"\xcd" * (payload_size * 2)  # exactly 2 full blocks

    tag = make_tag(
        [
            start_transfer_response(0),
            block_ack(1),
            block_ack(2),
        ]
    )
    tag._block_size = block_size

    asyncio.run(tag._transfer(framebuffer, block_size))

    # start + 2 blocks = 3 writes; loop exits on size check, no 3rd send.
    assert len(tag._client.calls) == 3


# -- upload() guard rails -----------------------------------------------------


def test_upload_without_connection_raises():
    tag = GiciskyTag("00:00:00:00:00:00")

    with pytest.raises(GiciskyConnectionError):
        asyncio.run(tag.upload(b"\x00"))
