"""纯 PIL 的 Minecraft 玩家模型 3D 预览（无 numpy / 无 three.js / 无浏览器）。

模型本质只有 6 个长方体（头、身、左右臂、左右腿）加 6 个可选覆盖层
（帽子、外套、袖子、裤腿）。正交投影后每个可见面是一个平行四边形，于是可以
逐面做最近邻仿射纹理映射（Image.transform(AFFINE)），再按面心深度做画家算法
叠加。亮度沿用游戏里的轴向着色：顶面 1.0、z 向侧面 0.8、x 向侧面 0.6、
底面 0.5，所以看起来就是游戏里的味道。

纹理展开规律（与实测坐标一致；坐标按 k = 表宽 / 64 等比放大，HD 皮肤同理）：
    top    (ox+d,     oy)     w×d
    bottom (ox+d+w,   oy)     w×d
    right  (ox,       oy+d)   d×h   —— 模型 -x 那一侧
    front  (ox+d,     oy+d)   w×h
    left   (ox+d+w,   oy+d)   d×h   —— 模型 +x 那一侧
    back   (ox+2d+w,  oy+d)   w×h
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageDraw

FACES = ("top", "bottom", "front", "back", "left", "right")

FACE_NORMAL = {
    "top": (0.0, 1.0, 0.0),
    "bottom": (0.0, -1.0, 0.0),
    "front": (0.0, 0.0, 1.0),
    "back": (0.0, 0.0, -1.0),
    "left": (1.0, 0.0, 0.0),
    "right": (-1.0, 0.0, 0.0),
}

FACE_LIGHT = {"top": 1.0, "bottom": 0.5, "front": 0.8, "back": 0.8, "left": 0.6, "right": 0.6}

# 覆盖层相对本体外扩半个像素（游戏里就是 0.5）
_INFLATE = 0.5
_GAP = 12
# 模型含帽子层高 33 单位，留一点余量
_MODEL_SPAN = 34.0


@dataclass(frozen=True)
class Part:
    name: str
    box: tuple[float, float, float, float, float, float]  # x0,y0,z0,x1,y1,z1
    tex: tuple[int, int]  # 展开图原点（64 表坐标）
    overlay_tex: tuple[int, int] | None = None
    mirror: bool = False  # 64x32 旧表：左臂/左腿复用右侧展开并镜像


def build_parts(slim: bool = False) -> tuple[Part, ...]:
    """slim=True 即 alex 模型，手臂宽 3 像素。

    展开原点与原版 ModelPlayer 一致，正好把 64x64 表铺成四行、互不重叠：
        y0-15  头 / 帽子      y16-31 右腿 / 身 / 右臂
        y32-47 右裤 / 外套 / 右袖  y48-63 左裤 / 左腿 / 左臂 / 左袖
    """
    aw = 3.0 if slim else 4.0
    return (
        Part("head", (-4.0, 24.0, -4.0, 4.0, 32.0, 4.0), (0, 0), (32, 0)),
        Part("body", (-4.0, 12.0, -2.0, 4.0, 24.0, 2.0), (16, 16), (16, 32)),
        Part("right_arm", (-4.0 - aw, 12.0, -2.0, -4.0, 24.0, 2.0), (40, 16), (40, 32)),
        Part("left_arm", (4.0, 12.0, -2.0, 4.0 + aw, 24.0, 2.0), (32, 48), (48, 48)),
        Part("right_leg", (-4.0, 0.0, -2.0, 0.0, 12.0, 2.0), (0, 16), (0, 32)),
        Part("left_leg", (0.0, 0.0, -2.0, 4.0, 12.0, 2.0), (16, 48), (0, 48)),
    )


def legacy_mirrors(parts: tuple[Part, ...], arm_w: float) -> tuple[Part, Part]:
    """64x32 旧表没有左半边，左臂/左腿按游戏的镜像规则复用右侧展开。"""
    right_arm, right_leg = parts[2], parts[4]
    x0, x1 = 4.0, 4.0 + arm_w
    return (
        Part("left_arm", (x0, 12.0, -2.0, x1, 24.0, 2.0), right_arm.tex, None, True),
        Part("left_leg", (x0, 0.0, -2.0, x1, 12.0, 2.0), right_leg.tex, None, True),
    )


def is_legacy(width: int, height: int) -> bool:
    """64x32 / 128x64 这类“只有一行半”的旧式表。"""
    return height * 2 == width


def model_parts(slim: bool = False, legacy: bool = False) -> tuple[Part, ...]:
    """2D 展开图与 3D 预览共用同一份模型定义，避免两处坐标各说各话。"""
    parts = list(build_parts(slim))
    if legacy:
        arm_w = parts[2].box[3] - parts[2].box[0]
        parts[3], parts[5] = legacy_mirrors(tuple(parts), arm_w)
    return tuple(parts)


def part_size(part: Part, k: float) -> tuple[int, int, int]:
    """部件在皮肤表里的像素尺寸（HD 表按 k 等比放大）。"""
    return (
        round((part.box[3] - part.box[0]) * k),
        round((part.box[4] - part.box[1]) * k),
        round((part.box[5] - part.box[2]) * k),
    )


def face_rects(ox: int, oy: int, w: int, h: int, d: int) -> dict[str, tuple[int, int, int, int]]:
    return {
        "top": (ox + d, oy, w, d),
        "bottom": (ox + d + w, oy, w, d),
        "right": (ox, oy + d, d, h),
        "front": (ox + d, oy + d, w, h),
        "left": (ox + d + w, oy + d, d, h),
        "back": (ox + 2 * d + w, oy + d, w, h),
    }


def _face_axes(
    box: tuple[float, float, float, float, float, float],
    name: str,
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    """返回 (贴图左上角对应的模型角点, u 方向向量, v 方向向量)。"""
    x0, y0, z0, x1, y1, z1 = box
    w, h, d = x1 - x0, y1 - y0, z1 - z0
    axes = {
        "x": (w, 0.0, 0.0),
        "nx": (-w, 0.0, 0.0),
        "ny": (0.0, -h, 0.0),
        "z": (0.0, 0.0, d),
        "nz": (0.0, 0.0, -d),
    }
    corners = {
        "top": ((x0, y1, z0), "x", "z"),
        "bottom": ((x0, y0, z1), "x", "nz"),
        "front": ((x0, y1, z1), "x", "ny"),
        "back": ((x1, y1, z0), "nx", "ny"),
        "left": ((x1, y1, z0), "z", "ny"),
        "right": ((x0, y1, z1), "nz", "ny"),
    }
    tl, u, v = corners[name]
    return tl, axes[u], axes[v]


def _inflate(box: tuple[float, ...]) -> tuple[float, ...]:
    x0, y0, z0, x1, y1, z1 = box
    return (x0 - _INFLATE, y0 - _INFLATE, z0 - _INFLATE, x1 + _INFLATE, y1 + _INFLATE, z1 + _INFLATE)


class _Camera:
    """正交相机：先绕 Y 转 yaw，再绕 X 转 pitch；屏幕 x 向右、y 向下，+z 朝向观察者。

    pitch > 0 表示从斜上方俯视（能看到头顶），这也是皮肤预览惯用的角度。
    """

    def __init__(self, yaw: float, pitch: float, scale: float) -> None:
        self.cy = math.cos(math.radians(yaw))
        self.sy = math.sin(math.radians(yaw))
        self.cp = math.cos(math.radians(pitch))
        self.sp = math.sin(math.radians(pitch))
        self.scale = scale

    def rot(self, p: tuple[float, float, float]) -> tuple[float, float, float]:
        x, y, z = p
        x1 = x * self.cy + z * self.sy
        z1 = z * self.cy - x * self.sy
        return (x1, y * self.cp - z1 * self.sp, y * self.sp + z1 * self.cp)

    def project(self, p: tuple[float, float, float]) -> tuple[float, float, float]:
        rx, ry, rz = self.rot(p)
        return (rx * self.scale, -ry * self.scale, rz)


def _shade(tile: Image.Image, factor: float) -> Image.Image:
    if factor >= 0.999:
        return tile
    r, g, b, a = tile.split()

    def darken(v: int) -> int:
        return min(255, int(v * factor + 0.5))

    return Image.merge("RGBA", (r.point(darken), g.point(darken), b.point(darken), a))


def _map_face(
    sheet: Image.Image,
    rect: tuple[int, int, int, int],
    quad: tuple[tuple[float, float], ...],
    light: float,
    flip: bool,
) -> tuple[Image.Image, int, int] | None:
    """把一个面贴进它自己的小平行四边形；quad = 贴图源矩形的左上/右上/右下/左下四点屏幕坐标。"""
    uw, uh = rect[2], rect[3]
    if uw <= 0 or uh <= 0:
        return None
    box = (rect[0], rect[1], rect[0] + uw, rect[1] + uh)
    (tlx, tly), (trx, tryy), _, (blx, bly) = quad
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    bx0 = int(math.floor(min(xs)))
    by0 = int(math.floor(min(ys)))
    tw = int(math.ceil(max(xs))) + 1 - bx0
    th = int(math.ceil(max(ys))) + 1 - by0
    if tw < 1 or th < 1:
        return None

    e1x, e1y = (trx - tlx) / uw, (tryy - tly) / uw
    e2x, e2y = (blx - tlx) / uh, (bly - tly) / uh
    det = e1x * e2y - e2x * e1y
    if abs(det) < 1e-9:  # 投影退化成一条线
        return None

    inv = 1.0 / det
    a, b = e2y * inv, -e2x * inv
    d, e = -e1y * inv, e1x * inv
    dx0, dy0 = bx0 - tlx, by0 - tly
    tex = sheet.crop(box).convert("RGBA")
    if flip:
        tex = tex.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    tile = tex.transform(
        (tw, th),
        Image.AFFINE,
        (a, b, a * dx0 + b * dy0, d, e, d * dx0 + e * dy0),
        resample=Image.Resampling.NEAREST,
    )

    mask = Image.new("L", (tw, th), 0)
    ImageDraw.Draw(mask).polygon([(x - bx0, y - by0) for x, y in quad], fill=255)
    alpha = ImageChops.multiply(tile.split()[3], mask)
    if alpha.getbbox() is None:  # 全透明，多半是覆盖层的空区域
        return None
    tile.putalpha(alpha)
    return _shade(tile, light), bx0, by0


def _view_blocks(
    cam: _Camera,
    sheet: Image.Image,
    parts: tuple[Part, ...],
    k: float,
    overlays: bool,
) -> list[tuple[float, Image.Image, int, int]]:
    """渲染一个视角，返回 (深度, 图块, x, y)。模型绕自身中心旋转。"""
    cx, cy, cz = 0.0, 16.0, 0.0
    blocks: list[tuple[float, Image.Image, int, int]] = []
    for part in parts:
        # 覆盖层只是几何上外扩，贴图仍复用本体的展开格
        w, h, d = part_size(part, k)
        layers = [(part.box, part.tex)]
        if overlays and part.overlay_tex is not None:
            layers.append((_inflate(part.box), part.overlay_tex))
        for box, (ox, oy) in layers:
            rects = face_rects(round(ox * k), round(oy * k), w, h, d)
            for name in FACES:
                if cam.rot(FACE_NORMAL[name])[2] <= 1e-9:
                    continue  # 背面剔除
                # 镜像件：±x 侧面直接换用对面的展开格（u 沿 z，不需翻转），
                # 其余四个面 u 沿 x，必须水平翻转贴图。
                src = {"left": "right", "right": "left"}.get(name, name) if part.mirror else name
                flip = part.mirror and src not in ("left", "right")
                tl, e1, e2 = _face_axes(box, name)
                pts = []
                zs = []
                for t, s in ((0, 0), (1, 0), (0, 1), (1, 1)):
                    p = (
                        tl[0] + e1[0] * t + e2[0] * s - cx,
                        tl[1] + e1[1] * t + e2[1] * s - cy,
                        tl[2] + e1[2] * t + e2[2] * s - cz,
                    )
                    sx, sy, sz = cam.project(p)
                    pts.append((sx, sy))
                    zs.append(sz)
                block = _map_face(
                    sheet,
                    rects[src],
                    (pts[0], pts[1], pts[3], pts[2]),
                    FACE_LIGHT[name],
                    flip,
                )
                if block is None:
                    continue
                blocks.append((sum(zs) / len(zs), *block))
    return blocks


DEFAULT_VIEWS = (0.0, 30.0, 180.0)


def render_views(
    sheet: Image.Image,
    slim: bool = False,
    views: tuple[float, ...] = DEFAULT_VIEWS,
    figure_height: int = 200,
    pitch: float = 12.0,
    scale: float | None = None,
) -> Image.Image:
    """渲染若干视角并横向拼接，返回 RGBA（透明底）。

    scale = 每个模型单位对应多少像素，皮肤预览拼版时按表宽给它，
    3D 人偶就能和纹理表保持同一套像素比例；没给才按 figure_height 折算。
    """
    if not views:
        raise ValueError("至少需要一个视角")
    k = sheet.width / 64.0
    legacy = is_legacy(sheet.width, sheet.height)
    parts = model_parts(slim, legacy)
    overlays = not legacy
    if scale is None:
        scale = max(48, min(int(figure_height), 1024)) / _MODEL_SPAN
    scale = max(0.25, min(float(scale), 64.0))

    tiles: list[Image.Image] = []
    for yaw in views:
        blocks = _view_blocks(_Camera(yaw, pitch, scale), sheet, parts, k, overlays)
        if not blocks:
            continue
        min_x = min(b[2] for b in blocks)
        min_y = min(b[3] for b in blocks)
        max_x = max(b[2] + b[1].width for b in blocks)
        max_y = max(b[3] + b[1].height for b in blocks)
        canvas = Image.new("RGBA", (int(max_x - min_x) + 2, int(max_y - min_y) + 2), (0, 0, 0, 0))
        for _depth, tile, bx, by in sorted(blocks, key=lambda item: item[0]):
            canvas.alpha_composite(tile, (int(bx - min_x) + 1, int(by - min_y) + 1))
        tiles.append(canvas)

    if not tiles:
        raise ValueError("这张皮肤没渲染出任何像素")
    out = Image.new("RGBA", (sum(t.width for t in tiles) + _GAP * (len(tiles) - 1), max(t.height for t in tiles)), (0, 0, 0, 0))
    x = 0
    for tile in tiles:
        out.alpha_composite(tile, (x, out.height - tile.height))
        x += tile.width + _GAP
    return out
