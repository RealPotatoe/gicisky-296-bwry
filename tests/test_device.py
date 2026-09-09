"""Protocol and upload regressions using an in-process BLE client."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from bleak.exc import BleakError
from PIL import Image

from gicisky_296_bwry import HEIGHT, WIDTH
from gicisky_296_bwry.device import CMD_UUID, IMG_UUID, GiciskyTag
from gicisky_296_bwry.exceptions import (
    GiciskyConnectionError,
    GiciskyProtocolError,
    GiciskyTransferError,
)

FRAMEBUFFER = b"\xab" * (WIDTH * HEIGHT // 4)


class FakeBleakClient:
    """Replies arrive before writes return; None models a missing reply."""

    def __init__(self, tag, responses):
        self.tag = tag
        self.responses = list(responses)
        self.calls = []
        self.is_connected = True
        self.disconnect_calls = 0
        self.writing = asyncio.Event()

    async def write_gatt_char(self, uuid, packet, response=True):
        self.calls.append((uuid, bytes(packet)))
        self.writing.set()
        await asyncio.sleep(0)
        data = self.responses.pop(0)
        if isinstance(data, Exception):
            raise data
        if data is not None:
            self.tag._notification_handler(self, None, bytearray(data))

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False
        self.tag._disconnected_handler(self)


def make_tag(responses, **options):
    tag = GiciskyTag("00:00:00:00:00:00", **options)
    tag._client = FakeBleakClient(tag, responses)
    return tag


def block_size_response(size):
    return b"\x01" + size.to_bytes(2, "little")


def block_ack(part):
    return b"\x05\x00" + part.to_bytes(4, "little")


def upload_responses():
    block_count = (len(FRAMEBUFFER) + 239) // 240
    return [
        block_size_response(244),
        b"\x02\x00",
        block_ack(0),
        *(block_ack(part) for part in range(1, block_count)),
        b"\x05\x08",
    ]


@pytest.mark.parametrize("buffer_type", [bytes, bytearray, memoryview])
def test_upload_sends_expected_commands_and_framebuffer(buffer_type):
    tag = make_tag(upload_responses())
    client = tag._client

    asyncio.run(tag.upload(buffer_type(FRAMEBUFFER)))

    assert client.calls[:3] == [
        (CMD_UUID, b"\x01"),
        (CMD_UUID, b"\x02" + len(FRAMEBUFFER).to_bytes(4, "little") + b"\x00" * 3),
        (CMD_UUID, b"\x03"),
    ]
    blocks = [packet for uuid, packet in client.calls if uuid == IMG_UUID]
    assert [int.from_bytes(packet[:4], "little") for packet in blocks] == list(
        range(len(blocks))
    )
    assert b"".join(packet[4:] for packet in blocks) == FRAMEBUFFER
    assert all(len(packet) == 244 for packet in blocks[:-1])
    assert tag._block_size == 244
    assert client.disconnect_calls == 0


@pytest.mark.parametrize("size", [8, 512])
def test_negotiated_block_size_boundaries(size):
    tag = make_tag([block_size_response(size)])
    assert asyncio.run(tag._request_block_size()) == size


@pytest.mark.parametrize(
    "step,response,expected",
    [
        (0, b"", GiciskyProtocolError),
        (0, b"\x01\x00", GiciskyProtocolError),
        (0, b"\x99\x00\x00", GiciskyProtocolError),
        (0, block_size_response(7), GiciskyProtocolError),
        (0, block_size_response(513), GiciskyProtocolError),
        (1, b"\x02", GiciskyProtocolError),
        (1, b"\x99\x00", GiciskyProtocolError),
        (1, b"\x02\x01", GiciskyTransferError),
        (2, b"\x05\x00", GiciskyProtocolError),
        (2, b"\x05\x00\x00\x00\x00", GiciskyProtocolError),
        (2, b"\x05\x01", GiciskyTransferError),
        (2, block_ack(999), GiciskyProtocolError),
        (3, b"\x05\x00", GiciskyProtocolError),
        (3, b"\x05\x00\x00\x00\x00", GiciskyProtocolError),
        (3, b"\x99\x00", GiciskyProtocolError),
        (3, b"\x05\x01", GiciskyTransferError),
    ],
)
def test_upload_rejects_bad_responses_and_disconnects(step, response, expected):
    responses = upload_responses()
    responses[step] = response
    tag = make_tag(responses)
    client = tag._client

    with pytest.raises(expected):
        asyncio.run(tag.upload(FRAMEBUFFER))

    assert client.disconnect_calls == 1
    assert tag._client is None
    assert tag._block_size is None


@pytest.mark.parametrize(
    "indexes,completion,size",
    [
        ([0, 1], None, 479),
        ([0, 1], 2, 479),
        ([0, 1], 2, 480),
        ([1], None, 480),
        ([0, 0, 1, 0, 1], None, 480),
    ],
)
def test_transfer_preserves_completion_resume_and_retransmission(
    indexes, completion, size
):
    responses = [block_ack(part) for part in indexes]
    responses.append(b"\x05\x08" if completion is None else block_ack(completion))
    tag = make_tag(responses)

    asyncio.run(tag._transfer(b"\xcd" * size, 244))

    blocks = [packet for uuid, packet in tag._client.calls if uuid == IMG_UUID]
    assert [int.from_bytes(packet[:4], "little") for packet in blocks] == indexes


@pytest.mark.parametrize(
    "responses",
    [
        [block_ack(3)],
        [block_ack(999)],
        [block_ack(0), block_ack(2)],
        [block_ack(0), block_ack(3)],
        [block_ack(0), block_ack(999)],
        [block_ack(0), b"\x05\x08"],
    ],
)
def test_transfer_rejects_invalid_progress(responses):
    tag = make_tag(responses)
    with pytest.raises(GiciskyProtocolError):
        asyncio.run(tag._transfer(b"\xcd" * 720, 244))


@pytest.mark.parametrize("indexes", [[0], [0, 1]])
@pytest.mark.parametrize("max_retries", [0, 2])
def test_transfer_bounds_repeated_requests_and_cycles(indexes, max_retries):
    tag = make_tag(
        [block_ack(part) for part in indexes * (max_retries + 2)],
        max_retries=max_retries,
    )
    with pytest.raises(GiciskyTransferError, match="Retry limit"):
        asyncio.run(tag._transfer(b"\xcd" * 720, 244))
    assert len(tag._client.calls) == 1 + len(indexes) * (max_retries + 1)


@pytest.mark.parametrize("part", [-1, 1])
def test_send_block_rejects_invalid_index(part):
    tag = make_tag([])
    with pytest.raises(GiciskyTransferError):
        asyncio.run(tag._send_block(b"\x00", 244, part))
    assert tag._client.calls == []


@pytest.mark.parametrize(
    "framebuffer,error",
    [
        (b"", ValueError),
        (FRAMEBUFFER + b"x", ValueError),
        (None, TypeError),
        ("text", TypeError),
    ],
    ids=["empty", "oversized", "none", "text"],
)
def test_upload_validates_input_before_ble_io(framebuffer, error):
    tag = make_tag([])
    with pytest.raises(error):
        asyncio.run(tag.upload(framebuffer))
    assert tag._client.calls == []
    assert tag._client.disconnect_calls == 0


@pytest.mark.parametrize("stale", [False, True])
def test_upload_requires_a_live_connection(stale):
    tag = make_tag([])
    tag._client.is_connected = False
    if not stale:
        tag._client = None
    with pytest.raises(GiciskyConnectionError):
        asyncio.run(tag.upload(FRAMEBUFFER))


def test_display_encodes_image_and_rejects_invalid_input():
    tag = make_tag(upload_responses())
    with pytest.raises(ValueError):
        asyncio.run(tag.display(Image.new("RGB", (10, 10))))
    assert tag._client.calls == []
    asyncio.run(tag.display(Image.new("RGB", (WIDTH, HEIGHT), "white")))
    data = b"".join(
        packet[4:] for uuid, packet in tag._client.calls if uuid == IMG_UUID
    )
    assert data == b"\x55" * len(FRAMEBUFFER)


@pytest.mark.parametrize("step", [0, 3])
@pytest.mark.parametrize(
    "error_type", [BleakError, OSError, TimeoutError, RuntimeError]
)
def test_ble_failures_are_chained_but_programming_errors_are_not_wrapped(
    step, error_type
):
    original = error_type("write failed")
    responses = upload_responses()
    responses[step] = original
    tag = make_tag(responses)
    client = tag._client
    expected = {TimeoutError: GiciskyProtocolError, RuntimeError: RuntimeError}.get(
        error_type, GiciskyConnectionError
    )
    with pytest.raises(expected) as raised:
        asyncio.run(tag.upload(FRAMEBUFFER))
    assert (
        raised.value if expected is RuntimeError else raised.value.__cause__
    ) is original
    assert client.disconnect_calls == 1
    assert tag._client is None


@pytest.mark.parametrize("phase", ["reply", "write", "transfer"])
def test_stalled_operations_are_bounded(phase):
    tag = make_tag(
        [None],
        notification_timeout=1 if phase == "transfer" else 0.01,
        transfer_timeout=0.01 if phase == "transfer" else 1,
    )
    client = tag._client

    async def stalled_write(*args, **kwargs):
        await asyncio.Event().wait()

    if phase == "write":
        client.write_gatt_char = stalled_write
    expected = GiciskyTransferError if phase == "transfer" else GiciskyProtocolError
    with pytest.raises(expected):
        asyncio.run(tag.upload(FRAMEBUFFER))
    assert client.disconnect_calls == 1


@pytest.mark.parametrize("failure", ["cancel", "disconnect"])
def test_interrupted_upload_preserves_error_and_cleans_up(failure):
    async def run():
        tag = make_tag([None])
        client = tag._client
        task = asyncio.create_task(tag.upload(FRAMEBUFFER))
        await asyncio.wait_for(client.writing.wait(), 1)
        if failure == "cancel":
            task.cancel()
            expected = asyncio.CancelledError
        else:
            client.is_connected = False
            tag._disconnected_handler(client)
            expected = GiciskyConnectionError
        with pytest.raises(expected):
            await asyncio.wait_for(task, 1)
        assert client.disconnect_calls == 1
        assert tag._client is None

    asyncio.run(run())


def test_cleanup_failure_does_not_mask_upload_error(caplog):
    original = BleakError("write failed")
    tag = make_tag([original])
    tag._client.disconnect = AsyncMock(side_effect=OSError("cleanup failed"))
    with pytest.raises(GiciskyConnectionError) as raised:
        asyncio.run(tag.upload(FRAMEBUFFER))
    assert raised.value.__cause__ is original
    assert tag._client is None
    assert "cleanup failed" in caplog.text


def test_stale_and_duplicate_notifications_do_not_replace_current_reply():
    async def run():
        tag = make_tag([])
        client = tag._client
        old_client = FakeBleakClient(tag, [])

        async def replies(*args, **kwargs):
            tag._notification_handler(old_client, None, bytearray(b"\x99"))
            tag._disconnected_handler(old_client)
            tag._notification_handler(client, None, bytearray(block_size_response(244)))
            tag._notification_handler(client, None, bytearray(b"\x99"))

        client.write_gatt_char = replies
        assert await tag._request_block_size() == 244

    asyncio.run(run())


def test_concurrent_uploads_are_serialized_and_mutable_input_is_copied():
    async def run():
        responses = upload_responses()
        tag = make_tag(responses * 2)
        image = bytearray(FRAMEBUFFER)
        async with tag._operation_lock:
            first = asyncio.create_task(tag.upload(image))
            await asyncio.sleep(0)
            image[:] = b"\x00" * len(image)
            second = asyncio.create_task(tag.upload(FRAMEBUFFER))
        await asyncio.gather(first, second)
        midpoint = len(responses)
        assert tag._client.calls[:midpoint] == tag._client.calls[midpoint:]
        assert tag._client.disconnect_calls == 0

    asyncio.run(run())


def test_disconnect_waits_for_upload_and_failure_blocks_queued_upload():
    async def run():
        tag = make_tag([BleakError("write failed")])
        client = tag._client
        results = await asyncio.gather(
            tag.upload(FRAMEBUFFER),
            tag.upload(FRAMEBUFFER),
            tag.disconnect(),
            return_exceptions=True,
        )
        assert all(isinstance(error, GiciskyConnectionError) for error in results[:2])
        assert results[2] is None
        assert len(client.calls) == 1
        assert client.disconnect_calls == 1

    asyncio.run(run())
