"""Generate original 64x64 pixel-art sprites for the desktop pet."""

from pathlib import Path

from PIL import Image, ImageDraw


SIZE = 64
PIXEL = 4
OUTPUT_DIR = Path(__file__).resolve().parent / "sprites"

SPRITES = {
    "idle": {
        "body": "#78d6a5",
        "shadow": "#318c74",
        "belly": "#b7f0c9",
        "eye": "#26334a",
        "mouth": "smile",
    },
    "happy": {
        "body": "#ffd166",
        "shadow": "#d38b35",
        "belly": "#fff0a8",
        "eye": "closed",
        "mouth": "big_smile",
    },
    "sleepy": {
        "body": "#91a8e8",
        "shadow": "#5666a8",
        "belly": "#c7d4ff",
        "eye": "sleepy",
        "mouth": "small",
    },
    "hungry": {
        "body": "#f28b82",
        "shadow": "#bb514f",
        "belly": "#ffc2a8",
        "eye": "worried",
        "mouth": "open",
    },
    "sassy": {
        "body": "#c49ae8",
        "shadow": "#8158b6",
        "belly": "#ead1ff",
        "eye": "wink",
        "mouth": "smirk",
    },
}


def pixel_box(draw: ImageDraw.ImageDraw, x: int, y: int, color: str) -> None:
    """Draw one crisp 4x4 pixel-art cell."""
    left = x * PIXEL
    top = y * PIXEL
    draw.rectangle((left, top, left + PIXEL - 1, top + PIXEL - 1), fill=color)


def draw_sprite(name: str, palette: dict[str, str]) -> Image.Image:
    """Build a transparent sprite from a small grid of colored cells."""
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    silhouette = {
        3: (6, 9),
        4: (4, 11),
        5: (3, 12),
        6: (2, 13),
        7: (2, 13),
        8: (1, 14),
        9: (1, 14),
        10: (1, 14),
        11: (1, 14),
        12: (2, 13),
        13: (2, 13),
    }

    for row, (start, end) in silhouette.items():
        for column in range(start, end + 1):
            pixel_box(draw, column, row + 1, palette["shadow"])
    for row, (start, end) in silhouette.items():
        for column in range(start + 1, end):
            pixel_box(draw, column, row, palette["body"])

    for row, (start, end) in ((7, (5, 10)), (8, (4, 11)), (9, (4, 11)), (10, (5, 10))):
        for column in range(start, end + 1):
            pixel_box(draw, column, row, palette["belly"])

    for x in (4, 11):
        pixel_box(draw, x, 14, palette["shadow"])
        pixel_box(draw, x + 1, 14, palette["shadow"])
    pixel_box(draw, 3, 5, "#fff4df")
    pixel_box(draw, 12, 5, "#fff4df")

    eye = palette["eye"]
    if eye == "closed":
        for x in (5, 10):
            pixel_box(draw, x, 7, "#26334a")
            pixel_box(draw, x + 1, 7, "#26334a")
    elif eye == "sleepy":
        for x in (5, 10):
            pixel_box(draw, x, 7, "#26334a")
            pixel_box(draw, x + 1, 7, "#26334a")
    elif eye == "worried":
        for x in (5, 10):
            pixel_box(draw, x, 7, "#26334a")
            pixel_box(draw, x, 8, "#26334a")
    elif eye == "wink":
        pixel_box(draw, 5, 7, "#26334a")
        pixel_box(draw, 6, 7, "#26334a")
        pixel_box(draw, 10, 7, "#26334a")
        pixel_box(draw, 10, 8, "#26334a")
    else:
        pixel_box(draw, 5, 7, eye)
        pixel_box(draw, 6, 7, eye)
        pixel_box(draw, 5, 8, eye)
        pixel_box(draw, 10, 7, eye)
        pixel_box(draw, 11, 7, eye)
        pixel_box(draw, 10, 8, eye)

    for x in (3, 12):
        pixel_box(draw, x, 9, "#f5a1a1")

    mouth = palette["mouth"]
    if mouth in ("smile", "big_smile"):
        for x, y in ((7, 9), (8, 10), (9, 9)):
            pixel_box(draw, x, y, "#a94e62")
        if mouth == "big_smile":
            pixel_box(draw, 7, 10, "#a94e62")
            pixel_box(draw, 9, 10, "#a94e62")
    elif mouth == "open":
        pixel_box(draw, 7, 9, "#a94e62")
        pixel_box(draw, 8, 9, "#a94e62")
        pixel_box(draw, 7, 10, "#a94e62")
        pixel_box(draw, 8, 10, "#a94e62")
    elif mouth == "smirk":
        pixel_box(draw, 8, 9, "#a94e62")
        pixel_box(draw, 9, 9, "#a94e62")
    else:
        pixel_box(draw, 8, 9, "#a94e62")

    if name == "sleepy":
        for x in (12, 13, 14):
            pixel_box(draw, x, 2 + (x % 2), "#ffffff")
    elif name == "hungry":
        pixel_box(draw, 13, 3, "#fff0a8")
        pixel_box(draw, 14, 3, "#fff0a8")

    return image


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, palette in SPRITES.items():
        output_path = OUTPUT_DIR / f"{name}.png"
        draw_sprite(name, palette).save(output_path, format="PNG", optimize=True)
        print(f"Generated {output_path}")


if __name__ == "__main__":
    main()
