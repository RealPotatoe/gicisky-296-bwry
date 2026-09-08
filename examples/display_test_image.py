"""
Example: send a four-colour test image to a Gicisky 296x128 BWRY tag.

Usage:
    python examples/display_test_image.py AA:BB:CC:DD:EE:FF
"""

import asyncio
import logging
import sys

from PIL import Image, ImageDraw

from gicisky_296_bwry import HEIGHT, WIDTH, GiciskyTag

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)


def make_test_image() -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(img)

    mid_x, mid_y = WIDTH // 2, HEIGHT // 2

    draw.rectangle((0, 0, mid_x - 1, mid_y - 1), fill="black")
    draw.rectangle((mid_x, 0, WIDTH - 1, mid_y - 1), fill="red")
    draw.rectangle((0, mid_y, mid_x - 1, HEIGHT - 1), fill="yellow")
    draw.rectangle((mid_x, mid_y, WIDTH - 1, HEIGHT - 1), fill="white")

    draw.text((10, 10), "BLACK", fill="white")
    draw.text((mid_x + 10, 10), "RED", fill="white")
    draw.text((10, mid_y + 10), "YELLOW", fill="black")
    draw.text((mid_x + 10, mid_y + 10), "WHITE", fill="black")

    return img


async def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <BLE_ADDRESS>")
        sys.exit(1)

    address = sys.argv[1]
    image = make_test_image()

    async with GiciskyTag(address) as tag:
        await tag.display(image)


if __name__ == "__main__":
    asyncio.run(main())
