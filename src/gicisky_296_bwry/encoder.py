"""
Pure image encoding for the Gicisky 296x128 BWRY e-paper display.

This module has no BLE/Bleak dependency. It only knows how to turn a
Pillow image into the 2-bit-per-pixel packed framebuffer format expected
by the tag's vendor protocol (device type 0x002E).
"""

from PIL import Image

from .exceptions import GiciskyError

WIDTH = 296
HEIGHT = 128

# 2-bit colour codes used by device type 0x002E.
BLACK = 0b00
WHITE = 0b01
YELLOW = 0b10
RED = 0b11


def encode(image: Image.Image) -> bytes:
    """
    Encode a PIL image into the native 296x128 BWRY framebuffer.

    The image must be exactly WIDTH x HEIGHT (296x128). The Gicisky
    device profile for this panel rotates the framebuffer by 90 degrees
    relative to the logical image, which is reproduced here.

    Returns a ``bytes`` object of ``WIDTH * HEIGHT // 4`` bytes, with
    four 2-bit pixels packed into each byte (MSB first).
    """
    if image.size != (WIDTH, HEIGHT):
        raise ValueError(f"Expected {WIDTH}x{HEIGHT} image, got {image.size}")

    image = image.convert("RGB")

    # Matches the device profile used by hass-gicisky for this exact
    # device type: rotation = 90.
    image = image.rotate(90, expand=True)

    width, height = image.size
    pixels = image.load()

    data = bytearray()
    current_byte = 0
    shift_counter = 3

    for y in range(height):
        for x in range(width):
            r, g, b = pixels[x, y]
            value = _classify_pixel(r, g, b)

            current_byte |= value << (shift_counter * 2)

            if shift_counter == 0:
                data.append(current_byte)
                current_byte = 0
                shift_counter = 3
            else:
                shift_counter -= 1

    if shift_counter != 3:
        data.append(current_byte)

    expected = width * height // 4

    if len(data) != expected:
        raise GiciskyError(f"Bad encoded size: {len(data)}, expected {expected}")

    return bytes(data)


def _classify_pixel(r: int, g: int, b: int) -> int:
    """
    Classify an RGB pixel into one of the four BWRY colour codes.

    Reproduces the same colour classification as hass-gicisky so that
    images produced by common tools (which quantize to black/white/
    red/yellow) map onto the correct 2-bit codes.
    """
    is_white = r > 128 and g > 128 and b > 128
    is_red = r > 128
    is_green = g > 128
    is_blue = b > 128

    if is_green and is_red and is_blue:
        is_green = False

    if is_red and is_white:
        is_red = False

    if is_green:
        return YELLOW
    if is_red:
        return RED
    if is_white:
        return WHITE
    return BLACK
