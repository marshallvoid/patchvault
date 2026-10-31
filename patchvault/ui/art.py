try:
    from PIL import Image, ImageDraw, ImageFilter, ImageOps, ImageTk
except ImportError:
    Image = ImageDraw = ImageFilter = ImageOps = ImageTk = None  # type: ignore[assignment]


def compose_banner(cover, background, size):
    width, height = size
    base = ImageOps.fit(background or cover, size, Image.LANCZOS)  # type: ignore[attr-defined]
    if background is None:
        base = base.filter(ImageFilter.GaussianBlur(10))
    base = Image.blend(base, Image.new("RGB", size), 0.4)
    if cover is not None:
        thumb = cover.copy()
        thumb.thumbnail((width // 2, height - 24), Image.LANCZOS)  # type: ignore[attr-defined]
        top = (height - thumb.height) // 2
        ImageDraw.Draw(base).rectangle((11, top - 1, 12 + thumb.width, top + thumb.height), outline="#00000080")
        base.paste(thumb, (12, top))
    return base
