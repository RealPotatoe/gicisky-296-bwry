"""Connection lifecycle regressions with a mocked Bleak client."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from bleak.exc import BleakError

from gicisky_296_bwry import device
from gicisky_296_bwry.device import CMD_UUID, GiciskyTag
from gicisky_296_bwry.exceptions import GiciskyConnectionError


def make_mock_client():
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.start_notify = AsyncMock()
    client.is_connected = True
    return client


@pytest.fixture
def setup(monkeypatch):
    client = make_mock_client()
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(device, "BleakClient", factory)
    return GiciskyTag("AA:BB:CC:DD:EE:FF"), client, factory


@pytest.mark.parametrize("connection_timeout", [None, 2.5])
def test_context_manager_and_idempotent_lifecycle(setup, connection_timeout):
    tag, client, factory = setup
    if connection_timeout is not None:
        tag = GiciskyTag(tag.address, connection_timeout=connection_timeout)

    async def run():
        async with tag as connected:
            assert connected is tag
            await asyncio.gather(tag.connect(), tag.connect())
            assert tag._client is client
        await tag.disconnect()

    asyncio.run(run())
    factory.assert_called_once_with(
        tag.address,
        disconnected_callback=tag._disconnected_handler,
        timeout=30 if connection_timeout is None else connection_timeout,
    )
    client.connect.assert_awaited_once()
    client.disconnect.assert_awaited_once()
    client.start_notify.assert_awaited_once()
    uuid, callback = client.start_notify.call_args.args
    assert uuid == CMD_UUID
    assert callback.func == tag._notification_handler
    assert callback.args == (client,)
    assert tag._client is None


@pytest.mark.parametrize("stage", ["connect", "start_notify", "disconnect"])
@pytest.mark.parametrize(
    "error_type",
    [BleakError, OSError, TimeoutError, RuntimeError, asyncio.CancelledError],
)
def test_lifecycle_failures_clear_state_and_preserve_cause(setup, stage, error_type):
    tag, client, _ = setup
    original = error_type("backend failed")
    getattr(client, stage).side_effect = original
    if stage == "disconnect":
        tag._client = client
        tag._block_size = 244
    operation = tag.disconnect if stage == "disconnect" else tag.connect
    expected = (
        error_type
        if error_type in (RuntimeError, asyncio.CancelledError)
        else GiciskyConnectionError
    )

    with pytest.raises(expected) as raised:
        asyncio.run(operation())

    assert (
        raised.value.__cause__ if expected is GiciskyConnectionError else raised.value
    ) is original
    assert tag._client is None
    assert tag._block_size is None
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("stage", ["connect", "start_notify", "disconnect"])
def test_lifecycle_timeouts_are_bounded(setup, stage):
    default_tag, client, _ = setup
    tag = GiciskyTag(default_tag.address, connection_timeout=0.01)

    async def stalled_operation(*args, **kwargs):
        await asyncio.Event().wait()

    getattr(client, stage).side_effect = stalled_operation
    if stage == "disconnect":
        tag._client = client
    operation = tag.disconnect if stage == "disconnect" else tag.connect
    with pytest.raises(GiciskyConnectionError) as raised:
        asyncio.run(operation())
    assert isinstance(raised.value.__cause__, TimeoutError)
    assert tag._client is None
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize(
    "name,value,error",
    [
        ("connection_timeout", 0, ValueError),
        ("notification_timeout", -1, ValueError),
        ("transfer_timeout", float("nan"), ValueError),
        ("transfer_timeout", float("inf"), ValueError),
        ("notification_timeout", None, TypeError),
        ("connection_timeout", True, TypeError),
        ("max_retries", -1, ValueError),
        ("max_retries", 1.5, TypeError),
        ("max_retries", True, TypeError),
    ],
)
def test_invalid_limits_are_rejected(name, value, error):
    with pytest.raises(error, match=name):
        GiciskyTag("AA:BB:CC:DD:EE:FF", **{name: value})


def test_setup_cleanup_failure_preserves_original_error(setup, caplog):
    tag, client, _ = setup
    original = BleakError("subscribe failed")
    client.start_notify.side_effect = original
    client.disconnect.side_effect = OSError("cleanup failed")
    with pytest.raises(GiciskyConnectionError) as raised:
        asyncio.run(tag.connect())
    assert raised.value.__cause__ is original
    assert tag._client is None
    assert "cleanup failed" in caplog.text


@pytest.mark.parametrize(
    "body_error", [None, ValueError("body failed"), asyncio.CancelledError()]
)
def test_context_cleanup_raises_only_if_body_succeeded(setup, body_error, caplog):
    tag, client, _ = setup
    original = BleakError("disconnect failed")
    client.disconnect.side_effect = original
    expected = GiciskyConnectionError if body_error is None else type(body_error)

    async def run():
        with pytest.raises(expected) as raised:
            async with tag:
                if body_error is not None:
                    raise body_error
        if body_error is None:
            assert raised.value.__cause__ is original
        else:
            assert raised.value is body_error
            assert "disconnect failed" in caplog.text
        assert tag._client is None

    asyncio.run(run())


def test_failed_setup_and_stale_connection_can_be_replaced(setup):
    tag, client, factory = setup
    client.start_notify.side_effect = BleakError("subscribe failed")

    async def run():
        with pytest.raises(GiciskyConnectionError):
            await tag.connect()
        assert tag._client is None
        current = make_mock_client()
        factory.return_value = current
        await tag.connect()
        assert tag._client is current
        current.is_connected = False
        replacement = make_mock_client()
        factory.return_value = replacement
        await tag.connect()
        assert tag._client is replacement
        current.disconnect.assert_awaited_once()
        await tag.disconnect()

    asyncio.run(run())


def test_connect_requires_a_live_backend_and_wraps_constructor_errors(setup):
    tag, client, factory = setup
    client.is_connected = False
    with pytest.raises(GiciskyConnectionError, match="Not connected"):
        asyncio.run(tag.connect())
    client.start_notify.assert_not_awaited()
    client.disconnect.assert_awaited_once()
    assert tag._client is None

    original = BleakError("adapter unavailable")
    factory.side_effect = original
    with pytest.raises(GiciskyConnectionError) as raised:
        asyncio.run(tag.connect())
    assert raised.value.__cause__ is original
    assert tag._client is None
