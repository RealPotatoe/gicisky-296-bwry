"""
Flash three side-by-side copies of Tux onto the 296x128 BWRY panel.

Usage:
    python examples/display_tux.py AA:BB:CC:DD:EE:FF
"""

import asyncio
import logging
import sys
from pathlib import Path

from PIL import Image

from gicisky_296_bwry import HEIGHT, WIDTH, GiciskyTag

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

TUX_PATH = Path(__file__).resolve().parent / "assets" / "tux.png"
COPIES = 3

# BWRY palette used for quantization.
# fmt: off
PALETTE = [
    0, 0, 0,        # black
    255, 255, 255,  # white
    255, 255, 0,    # yellow
    255, 0, 0,      # red
]
# fmt: on


def load_tux() -> Image.Image:
    tux = Image.open(TUX_PATH).convert("RGBA")

    # Flatten transparency onto a white background.
    flattened = Image.new("RGB", tux.size, "white")
    flattened.paste(tux, mask=tux.split()[3])
    return flattened


def make_tux_row(copies: int = COPIES) -> Image.Image:
    tux = load_tux()

    # Fit each copy into an equal-width slot, preserving aspect ratio.
    slot_w = WIDTH // copies
    scale = min(slot_w / tux.width, HEIGHT / tux.height)
    new_w = round(tux.width * scale)
    new_h = round(tux.height * scale)
    tux = tux.resize((new_w, new_h), Image.Resampling.LANCZOS)

    canvas = Image.new("RGB", (WIDTH, HEIGHT), "white")
    for i in range(copies):
        slot_x = i * slot_w
        x = slot_x + (slot_w - new_w) // 2
        y = (HEIGHT - new_h) // 2
        canvas.paste(tux, (x, y))

    # Quantize to the 4-colour BWRY palette using dithering.
    palette_img = Image.new("P", (1, 1))
    palette_img.putpalette(PALETTE + [0] * (768 - len(PALETTE)))

    quantized = canvas.quantize(palette=palette_img, dither=Image.Dither.FLOYDSTEINBERG)
    return quantized.convert("RGB")


async def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <BLE_ADDRESS>")
        sys.exit(1)

    address = sys.argv[1]

    image = make_tux_row()
    image.save("tux_preview.png")
    logger.info("Saved preview to tux_preview.png")

    async with GiciskyTag(address) as tag:
        await tag.display(image)


if __name__ == "__main__":
    asyncio.run(main())
