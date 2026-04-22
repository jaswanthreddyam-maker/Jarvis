"""Generate jarvis.ico from a source PNG image.

Usage:
    python scripts/generate_ico.py [path_to_png]
    
If no path is given, it generates a programmatic icon.
"""
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Pillow not found. Installing...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "Pillow"])
    from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT_ROOT / "jarvis.ico"


def generate_programmatic_icon() -> Image.Image:
    """Create a clean Jarvis icon programmatically."""
    from PIL import ImageDraw, ImageFont

    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer dark circle
    draw.ellipse([4, 4, size - 4, size - 4], fill=(18, 23, 29, 255))
    # Gold ring
    draw.ellipse([12, 12, size - 12, size - 12], fill=(184, 155, 92, 255))
    # Inner dark circle
    draw.ellipse([24, 24, size - 24, size - 24], fill=(14, 18, 24, 255))

    # Letter J
    try:
        font = ImageFont.truetype("arial.ttf", 100)
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), "J", font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) // 2 - bbox[0]
    ty = (size - th) // 2 - bbox[1]
    draw.text((tx, ty), "J", fill=(244, 236, 223, 255), font=font)

    return img


def main():
    if len(sys.argv) > 1:
        src_path = Path(sys.argv[1])
        if not src_path.exists():
            print(f"File not found: {src_path}")
            sys.exit(1)
        img = Image.open(src_path).convert("RGBA")
    else:
        img = generate_programmatic_icon()

    # Generate multi-resolution ICO
    sizes = [16, 24, 32, 48, 64, 128, 256]
    icons = [img.resize((s, s), Image.Resampling.LANCZOS) for s in sizes]
    icons[0].save(str(OUTPUT), format="ICO", sizes=[(s, s) for s in sizes], append_images=icons[1:])
    print(f"[BUILD] Icon saved: {OUTPUT}")


if __name__ == "__main__":
    main()
