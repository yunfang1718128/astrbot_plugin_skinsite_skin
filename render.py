"""纯 PIL 的可视化：最近邻放大 + 正面展开拼图（+ 可选 3D 预览条）。

部位坐标不再手写表格，而是从 preview3d 的模型定义现算，2D 展开与 3D 预览
因此永远说同一套话（改模型只改一处）。规律本身在 preview3d 里逐格验证过。
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from .preview3d import (
    DEFAULT_VIEWS,
    face_rects,
    is_legacy,
    model_parts,
    part_size,
    render_views,
)

GAP = 2
MARGIN = 4
BG = (32, 33, 36, 255)
GRID = (70, 72, 80, 255)


def upscale(image: Image.Image, factor: int) -> Image.Image:
    """最近邻整数倍放大。"""
    factor = max(1, min(int(factor), 64))
    if factor == 1:
        return image.copy()
    return image.resize(
        (image.width * factor, image.height * factor),
        resample=Image.Resampling.NEAREST,
    )


def _crop(image: Image.Image, box: tuple[int, int, int, int]) -> Image.Image | None:
    """按 (x, y, w, h) 裁一块；越界或空矩形返回 None。"""
    x, y, w, h = box
    if w <= 0 or h <= 0:
        return None
    x2, y2 = x + w, y + h
    if x < 0 or y < 0 or x2 > image.width or y2 > image.height:
        return None
    return image.crop((x, y, x2, y2)).convert("RGBA")


def flatten_front(image: Image.Image, slim: bool = False) -> Image.Image:
    """把各部位的正面（含覆盖层）裁出来横向拼接。"""
    k = image.width / 64.0
    tiles: list[Image.Image] = []
    for part in model_parts(slim, is_legacy(image.width, image.height)):
        w, h, d = part_size(part, k)
        piece: Image.Image | None = None
        for origin in (part.tex, part.overlay_tex):
            if origin is None:
                continue
            rect = face_rects(round(origin[0] * k), round(origin[1] * k), w, h, d)["front"]
            tile = _crop(image, rect)
            if tile is None:
                continue
            if part.mirror:
                tile = tile.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if piece is None:
                piece = tile
            else:
                piece.alpha_composite(tile)
        if piece is not None:
            tiles.append(piece)

    if not tiles:
        raise ValueError("这张皮肤表裁不出任何部位")
    height = max(tile.height for tile in tiles)
    width = sum(tile.width for tile in tiles) + GAP * (len(tiles) - 1) + MARGIN * 2
    canvas = Image.new("RGBA", (width, height + MARGIN * 2), BG)
    draw = ImageDraw.Draw(canvas)
    x = MARGIN
    for tile in tiles:
        y = MARGIN + (height - tile.height)
        canvas.alpha_composite(tile, (x, y))
        draw.rectangle([x - 1, y - 1, x + tile.width, y + tile.height], outline=GRID)
        x += tile.width + GAP
    return canvas


def preview_3d(
    image: Image.Image,
    slim: bool = False,
    views: tuple[float, ...] = DEFAULT_VIEWS,
    d_scale: float | None = None,
) -> Image.Image:
    """3D 预览条。d_scale = 每个模型单位多少像素，默认跟纹理表同一套比例。"""
    if d_scale is None:
        d_scale = max(1.0, 2.0 * image.width / 64.0)
    return render_views(image, slim=slim, views=views, scale=d_scale)


def render_preview(
    image: Image.Image,
    zoom: int = 10,
    flatten: bool = True,
    max_pixels: int = 4_000_000,
    three_d: bool = False,
    slim: bool = False,
    views: tuple[float, ...] = DEFAULT_VIEWS,
    sheet: bool = True,
) -> Image.Image:
    """自上而下：原始皮肤表、正面展开图、3D 预览，整体最近邻放大。

    max_pixels 同时约束输出图：2G 内存的机器上，HD 表 × 32 倍能撑出近亿像素，
    所以放大倍数先按像素预算压回去，宁可少放大也不炸内存。
    """
    zoom = max(1, min(int(zoom), 32))
    bands: list[Image.Image] = [image] if sheet else []
    if flatten:
        bands.append(flatten_front(image, slim))
    if three_d:
        bands.append(preview_3d(image, slim, views))
    if not bands:
        bands.append(image)
    width = max(band.width for band in bands)
    height = sum(band.height for band in bands) + 2 * (len(bands) - 1)
    budget = max(1, int(max_pixels))
    fit = int((budget / max(1, width * height)) ** 0.5)
    zoom = max(1, min(zoom, fit))
    if len(bands) == 1:
        return upscale(bands[0], zoom)
    composed = Image.new("RGBA", (width, height), BG)
    y = 0
    for band in bands:
        composed.alpha_composite(band, ((width - band.width) // 2, y))
        y += band.height + 2
    return upscale(composed, zoom)
