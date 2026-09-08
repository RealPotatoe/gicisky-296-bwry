import pytest
from PIL import Image, ImageDraw

from gicisky_296_bwry.encoder import HEIGHT, WIDTH, encode


def test_encoded_size_matches_expected():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    data = encode(image)
    assert len(data) == WIDTH * HEIGHT // 4


def test_rejects_wrong_size():
    image = Image.new("RGB", (10, 10), "white")
    with pytest.raises(ValueError):
        encode(image)


def test_marker_pixel_lands_in_correct_rotated_position():
    """
    Verify that encode() applies exactly the documented 90-degree
    rotation and packs bits in the expected order.

    We place a single black marker pixel on an otherwise all-white
    image, independently rotate the image ourselves with PIL to find
    where that marker *should* land, then check that encode()'s packed
    output has the black (0b00) code at the corresponding bit position
    and white (0b01) everywhere else. This guards against regressions
    like rotating the wrong direction, swapping width/height, or
    packing pixels in the wrong order.
    """
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    marker_x, marker_y = 5, 10
    image.putpixel((marker_x, marker_y), (0, 0, 0))

    data = encode(image)

    # Independently reproduce the rotation encode() applies, then
    # locate the marker in rotated space by direct pixel search.
    rotated = image.rotate(90, expand=True)
    rot_width, rot_height = rotated.size
    rotated_pixels = rotated.load()

    marker_positions = [
        (x, y)
        for y in range(rot_height)
        for x in range(rot_width)
        if rotated_pixels[x, y] == (0, 0, 0)
    ]

    assert len(marker_positions) == 1
    mx, my = marker_positions[0]

    index = my * rot_width + mx
    byte_index = index // 4
    shift = 3 - (index % 4)

    byte_value = data[byte_index]
    code = (byte_value >> (shift * 2)) & 0b11

    assert code == 0b00  # black

    # Every other 2-bit slot should be white.
    total_pixels = rot_width * rot_height
    for i in range(total_pixels):
        if i == index:
            continue
        b_idx = i // 4
        sh = 3 - (i % 4)
        c = (data[b_idx] >> (sh * 2)) & 0b11
        assert c == 0b01


@pytest.mark.parametrize(
    "color,expected_top_bits",
    [
        ("black", 0b00),
        ("white", 0b01),
        ("yellow", 0b10),
        ("red", 0b11),
    ],
)
def test_solid_color_encodes_to_expected_value(color, expected_top_bits):
    image = Image.new("RGB", (WIDTH, HEIGHT), color)
    data = encode(image)

    # Every byte should be four copies of the same 2-bit value.
    expected_byte = (
        expected_top_bits << 6
        | expected_top_bits << 4
        | expected_top_bits << 2
        | expected_top_bits
    )

    assert all(b == expected_byte for b in data)


def test_four_quadrants_does_not_raise():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    mid_x, mid_y = WIDTH // 2, HEIGHT // 2

    draw.rectangle((0, 0, mid_x - 1, mid_y - 1), fill="black")
    draw.rectangle((mid_x, 0, WIDTH - 1, mid_y - 1), fill="red")
    draw.rectangle((0, mid_y, mid_x - 1, HEIGHT - 1), fill="yellow")
    draw.rectangle((mid_x, mid_y, WIDTH - 1, HEIGHT - 1), fill="white")

    data = encode(image)
    assert len(data) == WIDTH * HEIGHT // 4
