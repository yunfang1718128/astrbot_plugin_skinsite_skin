"""生成 logo.png（512×512）：自绘一张示例皮肤纹理 → 用插件自己的 3D 渲染器出图 → 合成到圆角底板上。

不引用任何站点上的用户皮肤，避免版权问题。跑完就可以删。
"""

from __future__ import annotations

import os
import sys
import types

from PIL import Image, ImageDraw, ImageFilter

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(REPO))
pkg = types.ModuleType("littleskin_skin")
pkg.__path__ = [REPO]
sys.modules["littleskin_skin"] = pkg

from littleskin_skin.preview3d import face_rects, model_parts, part_size, render_views  # noqa: E402

SIZE = 512
HAIR = (38, 48, 62)
SKIN = (243, 205, 168)
SKIN_SHADE = (222, 178, 142)
SHIRT = (34, 150, 154)
SHIRT_DARK = (20, 104, 110)
PANTS = (58, 66, 84)
SHOE = (26, 30, 42)
EYE = (28, 36, 48)
ACCENT = (240, 176, 64)


def _rects(part, w: int, h: int, d: int, layer: tuple[int, int] | None) -> dict:
    ox, oy = layer or part.tex
    return face_rects(ox, oy, w, h, d)


def _box(rect) -> list[int]:
    x, y, w, h = rect
    return [x, y, x + w - 1, y + h - 1]


def build_sheet() -> Image.Image:
    """手画一张 64×64 皮肤表（只画用得到的格子）。"""
    sheet = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    dr = ImageDraw.Draw(sheet)
    parts = {p.name: p for p in model_parts(slim=False)}

    def paint(name: str, layer: tuple[int, int] | None = None) -> dict:
        part = parts[name]
        w, h, d = part_size(part, 1.0)
        return _rects(part, w, h, d, layer)

    # ---- 头：头发盖顶 + 脸 ----
    r = paint("head")
    dr.rectangle(_box(r["top"]), fill=HAIR + (255,))
    fx, fy, fw, fh = r["front"]
    dr.rectangle([fx, fy, fx + fw - 1, fy + 2], fill=HAIR + (255,))  # 刘海
    dr.rectangle([fx, fy + 3, fx + fw - 1, fy + fh - 1], fill=SKIN + (255,))
    dr.rectangle([fx + 1, fy + 4, fx + 2, fy + 5], fill=EYE + (255,))
    dr.rectangle([fx + fw - 3, fy + 4, fx + fw - 2, fy + 5], fill=EYE + (255,))
    dr.rectangle([fx + 3, fy + 6, fx + fw - 4, fy + 6], fill=SKIN_SHADE + (255,))
    for side in ("right", "left", "back"):
        sx, sy, sw, sh = r[side]
        if side == "back":
            dr.rectangle([sx, sy, sx + sw - 1, sy + 2], fill=HAIR + (255,))
            dr.rectangle([sx, sy + 3, sx + sw - 1, sy + sh - 1], fill=SKIN_SHADE + (255,))
        else:
            dr.rectangle([sx, sy, sx + sw - 1, sy + 2], fill=HAIR + (255,))
            dr.rectangle([sx, sy + 3, sx + sw - 1, sy + sh - 1], fill=SKIN + (255,))
    dr.rectangle(_box(r["bottom"]), fill=SKIN_SHADE + (255,))

    # ---- 身体：衬衫 + 领口 + 腰带 ----
    r = paint("body")
    for side in ("front", "back", "right", "left"):
        dr.rectangle(_box(r[side]), fill=(SHIRT if side in ("front", "right") else SHIRT_DARK) + (255,))
    dr.rectangle(_box(r["top"]), fill=SHIRT_DARK + (255,))
    dr.rectangle(_box(r["bottom"]), fill=PANTS + (255,))
    fx, fy, fw, fh = r["front"]
    dr.rectangle([fx + 2, fy, fx + fw - 3, fy + 1], fill=SKIN + (255,))  # 领口
    dr.rectangle([fx + 3, fy + 8, fx + fw - 4, fy + 8], fill=ACCENT + (255,))  # 腰带
    dr.rectangle([fx + 3, fy + 9, fx + fw - 4, fy + 9], fill=SHIRT_DARK + (255,))

    # ---- 手臂：短袖 + 皮肤 ----
    for name in ("right_arm", "left_arm"):
        r = paint(name)
        for face in ("front", "right", "left", "back"):
            sx, sy, sw, sh = r[face]
            top = 3
            dr.rectangle([sx, sy, sx + sw - 1, sy + top - 1], fill=SHIRT + (255,))
            dr.rectangle([sx, sy + top, sx + sw - 1, sy + sh - 1], fill=SKIN + (255,))
            dr.rectangle([sx, sy + sh - 1, sx + sw - 1, sy + sh - 1], fill=SKIN_SHADE + (255,))
        dr.rectangle(_box(r["top"]), fill=SHIRT_DARK + (255,))
        dr.rectangle(_box(r["bottom"]), fill=SKIN_SHADE + (255,))

    # ---- 腿：裤子 + 鞋 ----
    for name, pants in (("right_leg", PANTS), ("left_leg", (44, 50, 66))):
        r = paint(name)
        for face in ("front", "right", "left", "back"):
            sx, sy, sw, sh = r[face]
            dr.rectangle([sx, sy, sx + sw - 1, sy + sh - 4], fill=pants + (255,))
            dr.rectangle([sx, sy + sh - 3, sx + sw - 1, sy + sh - 1], fill=SHOE + (255,))
        dr.rectangle(_box(r["top"]), fill=pants + (255,))
        dr.rectangle(_box(r["bottom"]), fill=SHOE + (255,))

    return sheet


def background() -> Image.Image:
    """圆角深色底 + 径向提亮。"""
    bg = Image.new("L", (SIZE, SIZE), 0)
    px = bg.load()
    cx, cy = SIZE * 0.5, SIZE * 0.42
    for y in range(SIZE):
        for x in range(SIZE):
            d = min(1.0, (((x - cx) ** 2 + (y - cy) ** 2) ** 0.5) / (SIZE * 0.72))
            px[x, y] = int(74 - 52 * d)  # 中心亮 -> 边缘暗
    tint = Image.new("RGBA", (SIZE, SIZE), (18, 30, 38, 255))
    layer = Image.new("RGBA", (SIZE, SIZE), (74, 116, 128, 255))
    base = Image.composite(layer, tint, bg)
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=104, fill=255)
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    return out


def main() -> None:
    sheet = build_sheet()
    fig = render_views(sheet, slim=False, views=(25,), figure_height=440)
    fig = fig.crop(fig.getbbox()) if fig.getbbox() else fig

    canvas = background()

    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(
        [SIZE * 0.26, SIZE * 0.74, SIZE * 0.74, SIZE * 0.92], fill=(70, 210, 205, 80)
    )
    canvas.alpha_composite(glow.filter(ImageFilter.GaussianBlur(26)))

    fw, fh = fig.size
    scale = min(0.86 * SIZE / fw, 0.80 * SIZE / fh)
    fig = fig.resize((int(fw * scale), int(fh * scale)), Image.Resampling.NEAREST)
    pos = ((SIZE - fig.width) // 2, int(SIZE * 0.50 - fig.height / 2))
    halo = fig.filter(ImageFilter.GaussianBlur(9))
    tint = Image.new("RGBA", halo.size, (90, 225, 220, 0))
    tint.putalpha(halo.getchannel("A").point(lambda a: int(a * 0.55)))
    canvas.alpha_composite(tint, (pos[0] - 3, pos[1] + 3))
    canvas.alpha_composite(fig, pos)

    ImageDraw.Draw(canvas).rounded_rectangle(
        [3, 3, SIZE - 4, SIZE - 4], radius=101, outline=(130, 224, 222, 110), width=3
    )

    out = os.path.join(REPO, "logo.png")
    canvas.save(out)
    print("saved", out, canvas.size, os.path.getsize(out), "B")


if __name__ == "__main__":
    main()
