"""
Connection lifecycle tests for gicisky_296_bwry.device.GiciskyTag.

Unlike test_device.py (which drives the protocol methods directly with
a fake in-process client), these tests patch bleak.BleakClient itself
to verify connect()/disconnect()/context-manager behavior, including
exception wrapping and idempotency.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gicisky_296_bwry.device import CMD_UUID, GiciskyTag
from gicisky_296_bwry.exceptions import GiciskyConnectionError


def make_mock_client() -> MagicMock:
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.start_notify = AsyncMock()
    client.stop_notify = AsyncMock()
    client.is_connected = True
    return client


@patch("gicisky_296_bwry.device.BleakClient")
def test_connect_success(mock_bleak_client_cls):
    mock_client = make_mock_client()
    mock_bleak_client_cls.return_value = mock_client

    tag = GiciskyTag("AA:BB:CC:DD:EE:FF")
    asyncio.run(tag.connect())

    mock_bleak_client_cls.assert_called_once_with("AA:BB:CC:DD:EE:FF")
    mock_client.connect.assert_awaited_once()
    mock_client.start_notify.assert_awaited_once_with(
        CMD_UUID, tag._notification_handler
    )
    assert tag._client is mock_client


@patch("gicisky_296_bwry.device.BleakClient")
def test_connect_wraps_exception(mock_bleak_client_cls):
    mock_client = make_mock_client()
    mock_client.connect.side_effect = RuntimeError("boom")
    mock_bleak_client_cls.return_value = mock_client

    tag = GiciskyTag("AA:BB:CC:DD:EE:FF")

    with pytest.raises(GiciskyConnectionError):
        asyncio.run(tag.connect())

    assert tag._client is None


def test_connect_is_idempotent_when_already_connected():
    tag = GiciskyTag("AA:BB:CC:DD:EE:FF")
    sentinel_client = object()
    tag._client = sentinel_client

    # Should return immediately without touching BleakClient at all.
    asyncio.run(tag.connect())

    assert tag._client is sentinel_client


def test_disconnect_stops_notify_and_clears_state():
    tag = GiciskyTag("AA:BB:CC:DD:EE:FF")
    mock_client = make_mock_client()
    tag._client = mock_client
    tag._block_size = 244

    asyncio.run(tag.disconnect())

    mock_client.stop_notify.assert_awaited_once_with(CMD_UUID)
    mock_client.disconnect.assert_awaited_once()
    assert tag._client is None
    assert tag._block_size is None


def test_disconnect_when_not_connected_is_noop():
    tag = GiciskyTag("AA:BB:CC:DD:EE:FF")

    asyncio.run(tag.disconnect())  # must not raise

    assert tag._client is None


def test_async_context_manager_calls_connect_and_disconnect():
    tag = GiciskyTag("AA:BB:CC:DD:EE:FF")
    tag.connect = AsyncMock()
    tag.disconnect = AsyncMock()

    async def run():
        async with tag as ctx:
            assert ctx is tag

    asyncio.run(run())

    tag.connect.assert_awaited_once()
    tag.disconnect.assert_awaited_once()
