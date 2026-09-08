"""
Ad-hoc test: resize/quantize test.jpg to fit the 296x128 BWRY panel and
push it to a real tag.

Usage:
    python examples/display_photo.py AA:BB:CC:DD:EE:FF test.jpg
"""

import asyncio
import logging
import sys

from PIL import Image

from gicisky_296_bwry import HEIGHT, WIDTH, GiciskyTag

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


# fmt: off
PALETTE = [
    0, 0, 0,        # black
    255, 255, 255,  # white
    255, 255, 0,    # yellow
    255, 0, 0,      # red
]
# fmt: on


def prepare_image(path: str) -> Image.Image:
    image = Image.open(path).convert("RGB")

    # Fit into the panel while preserving aspect ratio, centered on white.
    fitted = Image.new("RGB", (WIDTH, HEIGHT), "white")
    src_ratio = image.width / image.height
    dst_ratio = WIDTH / HEIGHT

    if src_ratio > dst_ratio:
        new_w = WIDTH
        new_h = round(WIDTH / src_ratio)
    else:
        new_h = HEIGHT
        new_w = round(HEIGHT * src_ratio)

    resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    fitted.paste(resized, ((WIDTH - new_w) // 2, (HEIGHT - new_h) // 2))

    # Quantize to the 4-colour BWRY palette using dithering.
    palette_img = Image.new("P", (1, 1))
    palette_img.putpalette(PALETTE + [0] * (768 - len(PALETTE)))

    quantized = fitted.quantize(palette=palette_img, dither=Image.Dither.FLOYDSTEINBERG)
    return quantized.convert("RGB")


async def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <BLE_ADDRESS> <IMAGE_PATH>")
        sys.exit(1)

    address, image_path = sys.argv[1], sys.argv[2]

    image = prepare_image(image_path)
    image.save("test_preview.png")
    logger.info("Saved preview to test_preview.png")

    async with GiciskyTag(address) as tag:
        await tag.display(image)


if __name__ == "__main__":
    asyncio.run(main())
