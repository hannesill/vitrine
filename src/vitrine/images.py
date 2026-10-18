"""Images for ``show()``: what counts as one, and the files an image card keeps.

An image card stores each picture as files next to the card, never inside the
card index: the original (a file's own bytes, or a PNG of an array or PIL
image), a thumbnail, and, when there is an overlay, the overlay and its
thumbnail as RGBA PNGs. The card's preview only names those files, so a
gallery of chest X-rays costs the index a few hundred bytes per image and the
board loads thumbnails until someone opens one.

File names carry a hash of their content, so the browser may cache them for
good: replacing a card writes new names.

A markdown card keeps the local images it embeds the same way (see
``store_markdown_images``), so a study still shows them after the files move.
SVG files are sanitized like matplotlib's, and served with a policy that runs
no script.
"""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vitrine._types import Picture

if TYPE_CHECKING:
    import numpy as np
    from PIL import Image as PILImage

IMAGE_SUFFIXES = {
    ".png": "png",
    ".jpg": "jpg",
    ".jpeg": "jpg",
    ".webp": "webp",
    ".gif": "gif",
    ".svg": "svg",
}
MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "svg": "image/svg+xml",
}
# The name of a file an image card keeps, after "<card_id>." on disk.
CARD_FILE_RE = re.compile(
    r"^(image|thumb|overlay|overlaythumb)-\d+-[0-9a-f]{8}\.(png|jpg|webp|gif|svg)$"
)

THUMB_MAX_SIDE = 512
# An array smaller than this on either side is data, not a picture: it keeps
# its repr() card. Picture(...) shows any array as an image.
_MIN_ARRAY_SIDE = 16
_MASK_ALPHA = 0.45
# Distinct on dark and light images; a dict of masks takes them in order.
OVERLAY_COLORS = (
    "#e6194b",
    "#3cb44b",
    "#4363d8",
    "#f58231",
    "#42d4f4",
    "#f032e6",
    "#bfef45",
    "#911eb4",
)


def _is_pil(obj: object) -> bool:
    module = getattr(type(obj), "__module__", "") or ""
    return module.startswith("PIL") and hasattr(obj, "save") and hasattr(obj, "size")


def _is_ndarray(obj: object) -> bool:
    return type(obj).__module__ == "numpy" and type(obj).__name__ == "ndarray"


def _looks_like_picture_array(arr: Any) -> bool:
    shape = arr.shape
    if arr.ndim == 2:
        return min(shape) >= _MIN_ARRAY_SIDE
    if arr.ndim == 3 and shape[2] in (3, 4):
        return min(shape[:2]) >= _MIN_ARRAY_SIDE
    # Channels first, as torch keeps them (`_to_array_8bit` moves them last).
    if arr.ndim == 3 and shape[0] in (1, 3, 4) and shape[2] not in (1, 3, 4):
        return min(shape[1:]) >= _MIN_ARRAY_SIDE
    return False


def is_image(obj: object) -> bool:
    """Whether show() takes ``obj`` as one image."""
    if isinstance(obj, Picture) or _is_pil(obj):
        return True
    if isinstance(obj, Path):
        return obj.suffix.lower() in IMAGE_SUFFIXES
    if _is_ndarray(obj):
        return _looks_like_picture_array(obj)
    return False


def is_gallery(obj: object) -> bool:
    """Whether show() takes ``obj`` as a gallery: a non-empty list of images."""
    return (
        isinstance(obj, (list, tuple))
        and len(obj) > 0
        and all(is_image(item) for item in obj)
    )


def _short_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8]


def _to_array_8bit(arr: Any) -> np.ndarray:
    """An array as uint8, channels last.

    Bool becomes 0/255. Any other non-8-bit array (16-bit radiographs, floats)
    is scaled to 8 bits between its 0.5th and 99.5th percentiles, which keeps
    a few extreme pixels from washing the picture out.
    """
    import numpy as np

    arr = np.asarray(arr)
    if arr.ndim == 3 and arr.shape[0] in (1, 3, 4) and arr.shape[2] not in (1, 3, 4):
        arr = np.moveaxis(arr, 0, -1)  # channels first, as torch keeps them
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[..., 0]
    if arr.dtype == bool:
        return arr.astype(np.uint8) * 255
    if arr.dtype == np.uint8:
        return arr
    values = arr.astype(np.float64)
    finite = values[np.isfinite(values)]
    lo, hi = np.percentile(finite, [0.5, 99.5]) if finite.size else (0.0, 1.0)
    if hi <= lo:
        hi = lo + 1.0
    scaled = np.clip((np.nan_to_num(values, nan=lo) - lo) / (hi - lo), 0.0, 1.0)
    return (scaled * 255).round().astype(np.uint8)


def _open(source: Any) -> tuple[PILImage.Image, bytes | None, str | None]:
    """A PIL image for ``source``, plus a file's own bytes and format, if any."""
    from PIL import Image

    if isinstance(source, (str, Path)):
        path = Path(source).expanduser()
        fmt = IMAGE_SUFFIXES.get(path.suffix.lower())
        if fmt is None:
            raise ValueError(
                f"Vitrine shows PNG, JPEG, WebP, GIF, and SVG files, not {path.suffix or path.name!r}. "
                "Convert other formats (such as DICOM) to an array or a PNG first."
            )
        if fmt == "svg":
            raise ValueError("An SVG can be a picture, but not an overlay.")
        data = path.read_bytes()
        image = Image.open(io.BytesIO(data))
        image.load()
        # Browsers turn a photo upright by its EXIF orientation; the thumbnail,
        # size and overlay canvas follow the picture as it is seen. The original
        # keeps its bytes, orientation tag included.
        return _upright(image), data, fmt
    if _is_pil(source):
        return _upright(source), None, None
    if _is_ndarray(source):
        return Image.fromarray(_to_array_8bit(source)), None, None
    raise TypeError(
        f"Cannot show {type(source).__name__} as an image: pass a PIL image, a NumPy "
        "array, or the path of an image file."
    )


def _upright(image: PILImage.Image) -> PILImage.Image:
    """``image`` turned as its EXIF orientation says, or itself when it says nothing."""
    from PIL import ImageOps

    orientation = image.getexif().get(0x0112, 1)
    return image if orientation in (0, 1) else ImageOps.exif_transpose(image)


def _displayable(image: PILImage.Image) -> PILImage.Image:
    """``image`` in a mode a browser shows the same way: L, RGB, or RGBA."""
    if image.mode in ("L", "RGB", "RGBA"):
        return image
    if image.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        from PIL import Image

        return Image.fromarray(_to_array_8bit(_as_array(image)))
    if image.mode in ("LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        return image.convert("RGBA")
    return image.convert("RGB")


def _as_array(image: PILImage.Image) -> np.ndarray:
    import numpy as np

    return np.asarray(image)


def _png(image: PILImage.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _thumbnail(image: PILImage.Image, *, smooth: bool = True) -> tuple[bytes, str]:
    from PIL import Image

    thumb = image.copy()
    thumb.thumbnail(
        (THUMB_MAX_SIDE, THUMB_MAX_SIDE),
        Image.Resampling.LANCZOS if smooth else Image.Resampling.NEAREST,
    )
    if thumb.mode == "RGBA":
        return _png(thumb), "png"
    buf = io.BytesIO()
    thumb.save(buf, format="JPEG", quality=85)
    return buf.getvalue(), "jpg"


def _hex_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _mask_layer(mask: Any, size: tuple[int, int], color: str) -> PILImage.Image:
    """An RGBA layer with ``color`` where ``mask`` is set, resized to ``size``."""
    import numpy as np
    from PIL import Image

    arr = np.asarray(mask)
    if arr.ndim == 3:
        arr = arr[..., 0] if arr.shape[2] in (1, 3, 4) else arr[0]
    on = Image.fromarray((arr > 0).astype(np.uint8) * 255).resize(
        size, Image.Resampling.NEAREST
    )
    layer = Image.new("RGBA", size, (*_hex_rgb(color), 0))
    layer.putalpha(on.point(lambda v: round(v * _MASK_ALPHA)))
    return layer


def _heatmap_layer(values: Any, size: tuple[int, int]) -> PILImage.Image:
    """An RGBA layer from 0-1 values: transparent at 0, red to yellow as it rises."""
    import numpy as np
    from PIL import Image

    v = np.clip(np.nan_to_num(np.asarray(values, dtype=np.float64)), 0.0, 1.0)
    rgba = np.zeros((*v.shape, 4), dtype=np.uint8)
    rgba[..., 0] = 255
    rgba[..., 1] = (v * 230).astype(np.uint8)
    rgba[..., 3] = (v * 0.75 * 255).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA").resize(size, Image.Resampling.BILINEAR)


def _overlay(
    overlay: Any, size: tuple[int, int], palette: dict[str, str]
) -> tuple[PILImage.Image, list[list[str]]]:
    """The overlay as one RGBA image of ``size``, and its legend.

    Named masks take their colour from ``palette``, shared by a gallery's
    pictures, so one name has one colour on every picture and in the legend.
    """
    import numpy as np
    from PIL import Image

    if isinstance(overlay, dict):
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        legend = []
        for name, mask in overlay.items():
            color = palette.setdefault(
                str(name), OVERLAY_COLORS[len(palette) % len(OVERLAY_COLORS)]
            )
            canvas = Image.alpha_composite(canvas, _mask_layer(mask, size, color))
            legend.append([str(name), color])
        return canvas, legend
    if isinstance(overlay, (str, Path)) or _is_pil(overlay):
        image, _, _ = _open(overlay)
        if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
            return image.convert("RGBA").resize(size, Image.Resampling.BILINEAR), []
        overlay = np.asarray(image.convert("L"))
    arr = np.asarray(overlay)
    if arr.ndim == 3 and arr.shape[2] == 4:
        return Image.fromarray(_to_array_8bit(arr), "RGBA").resize(
            size, Image.Resampling.BILINEAR
        ), []
    if arr.dtype == bool or (
        np.issubdtype(arr.dtype, np.integer) and set(np.unique(arr)) <= {0, 1, 255}
    ):
        return _mask_layer(arr, size, OVERLAY_COLORS[0]), []
    if np.issubdtype(arr.dtype, np.integer):
        arr = arr / 255.0
    return _heatmap_layer(arr, size), []


def _label_text(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3g}"
    return str(value)


# A width or height in pixels (or unitless); a percentage or another unit is
# relative and leaves the size to the viewBox.
_SVG_LENGTH_RE = r'\s{attr}\s*=\s*["\']\s*([0-9.]+)\s*(?:px)?\s*["\']'
_SVG_VIEWBOX_RE = re.compile(
    r'\sviewBox\s*=\s*["\']\s*[-0-9.]+[\s,]+[-0-9.]+[\s,]+([0-9.]+)[\s,]+([0-9.]+)'
)
_SVG_DEFAULT_SIZE = (1000, 750)


def _svg_file(path: Path) -> tuple[bytes, tuple[int, int]]:
    """A sanitized SVG file and its size in pixels, from width/height or the viewBox."""
    from vitrine.renderer import _sanitize_svg

    data = _sanitize_svg(path.expanduser().read_bytes())
    head = data[:4096].decode("utf-8", errors="replace")
    root = head[head.find("<svg") :] if "<svg" in head else head
    root = root[: root.find(">") + 1] if ">" in root else root
    size = [
        re.search(_SVG_LENGTH_RE.format(attr=attr), root)
        for attr in ("width", "height")
    ]
    if all(size):
        return data, (round(float(size[0].group(1))), round(float(size[1].group(1))))
    box = _SVG_VIEWBOX_RE.search(root)
    if box:
        return data, (round(float(box.group(1))), round(float(box.group(2))))
    return data, _SVG_DEFAULT_SIZE


def _is_svg_path(source: Any) -> bool:
    return isinstance(source, (str, Path)) and Path(source).suffix.lower() == ".svg"


def store_pictures(
    items: Iterable[Any], card_id: str, write: Any
) -> list[dict[str, Any]]:
    """Write each item's files with ``write(card_id, name, data)``; return their preview entries."""
    entries = []
    palette: dict[str, str] = {}
    for i, item in enumerate(items):
        picture = item if isinstance(item, Picture) else Picture(image=item)
        if _is_svg_path(picture.image):
            # A vector picture is its own thumbnail.
            data, size = _svg_file(Path(picture.image))
            entry: dict[str, Any] = {
                "file": f"image-{i}-{_short_hash(data)}.svg",
                "width": size[0],
                "height": size[1],
            }
            entry["thumb"] = entry["file"]
            write(card_id, entry["file"], data)
        else:
            image, data, fmt = _open(picture.image)
            shown = _displayable(image)
            size = shown.size
            if data is None:
                data, fmt = _png(shown), "png"
            entry = {
                "file": f"image-{i}-{_short_hash(data)}.{fmt}",
                "width": shown.width,
                "height": shown.height,
            }
            write(card_id, entry["file"], data)
            thumb, thumb_fmt = _thumbnail(shown)
            entry["thumb"] = f"thumb-{i}-{_short_hash(thumb)}.{thumb_fmt}"
            write(card_id, entry["thumb"], thumb)

        if picture.overlay is not None:
            layer, legend = _overlay(picture.overlay, size, palette)
            overlay_png = _png(layer)
            entry["overlay"] = f"overlay-{i}-{_short_hash(overlay_png)}.png"
            write(card_id, entry["overlay"], overlay_png)
            overlay_thumb, _ = _thumbnail(layer)
            entry["overlay_thumb"] = (
                f"overlaythumb-{i}-{_short_hash(overlay_thumb)}.png"
            )
            write(card_id, entry["overlay_thumb"], overlay_thumb)
            if legend:
                entry["legend"] = legend
        if picture.caption:
            entry["caption"] = str(picture.caption)
        if picture.labels:
            entry["labels"] = {
                str(k): _label_text(v) for k, v in picture.labels.items()
            }
        entries.append(entry)
    return entries


# Local images a markdown card embeds: ![alt](path "title") and <img src="path">.
_MD_IMAGE_RE = re.compile(
    r'(!\[[^\]]*\]\(\s*)(?:<([^>\n]+)>|([^)\s]+))((?:\s+"[^"]*")?\s*\))'
)
_HTML_IMAGE_RE = re.compile(
    r'(<img\b[^>]*?\bsrc\s*=\s*["\'])([^"\']+)(["\'])', re.IGNORECASE
)
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
# What a markdown card's text says in place of a local image; the board and
# the exports turn it into the file's address or its data.
MARKDOWN_IMAGE_SCHEME = "vitrine-image:"


def store_markdown_images(text: str, card_id: str, write: Any) -> tuple[str, list[str]]:
    """Copy the local images ``text`` embeds into the card; return the rewritten text and file names.

    Paths are read relative to the working directory. Web addresses, data URIs,
    and paths that are not image files stay as they are.
    """
    names: dict[str, str] = {}

    def keep(src: str) -> str:
        if _URL_SCHEME_RE.match(src) or src.startswith(("/api/", "#")):
            return src
        path = Path(src).expanduser()
        if path.suffix.lower() not in IMAGE_SUFFIXES or not path.is_file():
            return src
        key = str(path.resolve())
        if key not in names:
            if path.suffix.lower() == ".svg":
                data, _ = _svg_file(path)
            else:
                data = path.read_bytes()
            name = f"image-{len(names)}-{_short_hash(data)}.{IMAGE_SUFFIXES[path.suffix.lower()]}"
            write(card_id, name, data)
            names[key] = name
        return MARKDOWN_IMAGE_SCHEME + names[key]

    # `<path with spaces>` is markdown's way to write a destination with spaces.
    text = _MD_IMAGE_RE.sub(
        lambda m: (
            m.group(1)
            + (f"<{keep(m.group(2))}>" if m.group(2) is not None else keep(m.group(3)))
            + m.group(4)
        ),
        text,
    )
    text = _HTML_IMAGE_RE.sub(
        lambda m: m.group(1) + keep(m.group(2)) + m.group(3), text
    )
    return text, list(names.values())
