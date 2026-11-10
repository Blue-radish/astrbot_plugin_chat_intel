"""Export a 256px plugin icon and a lightweight 200px README image."""

import argparse
from pathlib import Path

from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parent.parent


def sync_logo(source: Path, docs_dir: Path) -> None:
    """Export matching transparent PNGs for AstrBot and the README.

    Args:
        source: Source image with an alpha channel.
        docs_dir: Directory receiving the README logo.
    """
    with Image.open(source) as original:
        image = original.convert("RGBA")
    bounds = image.getchannel("A").getbbox()
    if bounds is None:
        raise ValueError("The source logo is fully transparent.")
    image = ImageOps.contain(image.crop(bounds), (224, 224), Image.Resampling.LANCZOS)
    icon = Image.new("RGBA", (256, 256))
    icon.alpha_composite(image, ((256 - image.width) // 2, (256 - image.height) // 2))
    icon.save(ROOT / "logo.png", optimize=True)
    docs_dir.mkdir(parents=True, exist_ok=True)
    target = docs_dir / "logo.png"
    if target.resolve() != (ROOT / "logo.png").resolve():
        icon.resize((200, 200), Image.Resampling.LANCZOS).save(target, optimize=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "logo.png")
    args = parser.parse_args()
    sync_logo(args.source, ROOT / "docs")
