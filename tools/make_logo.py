"""生成 logo.png（512×512）—— 用官方 Steve 皮肤 + 插件自己的 3D 渲染器出 3 个候选。

用法：
    py tools/make_logo.py --variants      # 出候选图到 .logo_preview/
    py tools/make_logo.py --pick 2        # 把候选 2 存成 logo.png

注意：默认皮肤纹理版权归 Mojang（Minecraft 使用指南允许非商业同人使用，但不得暗示官方背书）。
如果要完全规避，可把 --skin 指向自己画的 64×64 皮肤表。
"""

from __future__ import annotations

import argparse
import os
import sys
import types

from PIL import Image, ImageDraw, ImageFilter

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO))
_pkg = types.ModuleType("littleskin_skin")
_pkg.__path__ = [REPO]
sys.modules["littleskin_skin"] = _pkg

from littleskin_skin.preview3d import render_views  # noqa: E402

SIZE = 512
DEFAULT_SKIN = os.path.join(REPO, ".tmp_steve.png")
PREVIEW_DIR = os.path.join(REPO, ".logo_preview")


# --------------------------------------------------------------------- 画布


def background(mode: str = "spot") -> Image.Image:
    """圆角深色底：mode=spot 中心聚光 / mode=flat 斜向渐变。"""
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 255))
    px = base.load()
    cx, cy = SIZE * 0.5, SIZE * 0.44
    for y in range(SIZE):
        for x in range(SIZE):
            if mode == "flat":
                t = (x * 0.35 + y * 0.65) / SIZE
                r = int(34 + 16 * t)
                g = int(46 + 22 * t)
                b = int(62 + 26 * t)
            else:
                d = min(1.0, (((x - cx) ** 2 + (y - cy) ** 2) ** 0.5) / (SIZE * 0.66))
                r = int(64 - 46 * d)
                g = int(84 - 58 * d)
                b = int(96 - 64 * d)
            px[x, y] = (r, g, b, 255)
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=110, fill=255)
    canvas.paste(base, (0, 0), mask)
    return canvas


def shadow(canvas: Image.Image, center_x: int, feet_y: int, width: int) -> None:
    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse(
        [center_x - width / 2, feet_y - width * 0.16, center_x + width / 2, feet_y + width * 0.16],
        fill=(0, 0, 0, 120),
    )
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(14)))


def halo(
    canvas: Image.Image, fig: Image.Image, pos: tuple[int, int], color=(120, 235, 230), alpha=0.34
) -> None:
    """轮廓光：必须画在人偶**之前**，否则会把本体模糊掉。"""
    blur = fig.filter(ImageFilter.GaussianBlur(7))
    tint = Image.new("RGBA", blur.size, color + (0,))
    tint.putalpha(blur.getchannel("A").point(lambda a: int(a * alpha)))
    canvas.alpha_composite(tint, (pos[0], pos[1] + 2))


def frame(canvas: Image.Image, color=(126, 214, 220, 120)) -> None:
    ImageDraw.Draw(canvas).rounded_rectangle(
        [3, 3, SIZE - 4, SIZE - 4], radius=107, outline=color, width=3
    )


def fit(fig: Image.Image, box_h: float, cy: float) -> tuple[Image.Image, tuple[int, int]]:
    """把人偶按高度比例缩放到画布内，返回（缩放后的人偶, 落点）。"""
    bbox = fig.getbbox()
    if bbox:
        fig = fig.crop(bbox)
    fw, fh = fig.size
    scale = min(0.92 * SIZE / fw, box_h * SIZE / fh)
    fig = fig.resize((max(1, int(fw * scale)), max(1, int(fh * scale))), Image.Resampling.NEAREST)
    return fig, ((SIZE - fig.width) // 2, int(SIZE * cy - fig.height / 2))


def draw_figure(
    canvas: Image.Image,
    raw: Image.Image,
    box_h: float,
    cy: float,
    glow: tuple[int, int, int] = (120, 235, 230),
    glow_alpha: float = 0.34,
) -> None:
    """阴影 → 轮廓光 → 本体，顺序不能反。"""
    fig, pos = fit(raw, box_h, cy)
    shadow(canvas, pos[0] + fig.width // 2, pos[1] + fig.height - int(fig.height * 0.02), int(fig.width * 1.05))
    halo(canvas, fig, pos, color=glow, alpha=glow_alpha)
    canvas.alpha_composite(fig, pos)


def figure(skin: Image.Image, view: float, height_px: int = 520) -> Image.Image:
    return render_views(skin, slim=False, views=(view,), figure_height=height_px)


def prepare_skin(skin: Image.Image) -> Image.Image:
    """给两条腿加一点“缝”：正交正面下同色双腿会粘成一块，暗化内侧看起来才分得开。

    这只是给 logo 用的衍生图，不改官方纹理文件本身。
    """
    from littleskin_skin.preview3d import face_rects, model_parts, part_size

    out = skin.copy()
    parts = {p.name: p for p in model_parts(slim=False)}
    for name, factor, inner_side in (
        ("right_leg", 0.90, "left"),   # 右腿内侧（靠左）压暗
        ("left_leg", 0.94, "right"),   # 左腿整体略暗
    ):
        part = parts[name]
        w, h, d = part_size(part, 1.0)
        rects = face_rects(part.tex[0], part.tex[1], w, h, d)
        for face, rect in rects.items():
            x, y, fw, fh = rect
            for yy in range(y, y + fh):
                for xx in range(x, x + fw):
                    r, g, b, a = out.getpixel((xx, yy))
                    k = factor
                    # 内侧那一列画成一道暗缝，否则正交正面下两条腿会粘成一块
                    if face == "front" and inner_side == "left" and xx <= x + 0:
                        k *= 0.62
                    if face == "front" and inner_side == "right" and xx >= x + fw - 1:
                        k *= 0.62
                    out.putpixel((xx, yy), (int(r * k), int(g * k), int(b * k), a))
    return out


# --------------------------------------------------------------------- 候选


def variant_1(skin: Image.Image) -> Image.Image:
    """正面大图，极简。"""
    canvas = background("spot")
    draw_figure(canvas, figure(skin, 0), 0.84, 0.50, glow=(120, 220, 235), glow_alpha=0.26)
    frame(canvas)
    return canvas


def variant_2(skin: Image.Image) -> Image.Image:
    """20° 斜侧 + 青色轮廓光（最有“产品图”感）。"""
    canvas = background("spot")
    draw_figure(canvas, figure(skin, 20), 0.82, 0.50, glow=(140, 245, 240), glow_alpha=0.38)
    frame(canvas, color=(140, 230, 230, 140))
    return canvas


def variant_3(skin: Image.Image) -> Image.Image:
    """正面 + 底部 SKIN 字标（能让人一眼知道是干嘛的）。"""
    canvas = background("flat")
    draw_figure(canvas, figure(skin, 0), 0.74, 0.44, glow=(120, 225, 220), glow_alpha=0.24)

    dr = ImageDraw.Draw(canvas)
    font = None
    for path in (
        r"C:\Windows\Fonts\seguisb.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        r"C:\Windows\Fonts\msyhbd.ttc",
    ):
        if os.path.exists(path):
            from PIL import ImageFont

            try:
                font = ImageFont.truetype(path, 76)
                break
            except OSError:
                continue
    if font:
        text = "SKIN"
        box = dr.textbbox((0, 0), text, font=font)
        tw = box[2] - box[0]
        dr.text(
            ((SIZE - tw) // 2, int(SIZE * 0.845)),
            text,
            font=font,
            fill=(236, 244, 246, 235),
            stroke_width=2,
            stroke_fill=(20, 30, 38, 200),
        )
    frame(canvas)
    return canvas


def variant_4(skin: Image.Image) -> Image.Image:
    """半身特写（插件列表里缩小时最好认）：底部渐变淡出。"""
    canvas = background("spot")
    raw = figure(skin, 12, height_px=1200)
    bbox = raw.getbbox()
    if bbox:
        raw = raw.crop(bbox)
    fw, fh = raw.size
    bust = raw.crop((0, 0, fw, int(fh * 0.66)))
    bw, bh = bust.size
    scale = min(0.86 * SIZE / bw, 0.84 * SIZE / bh)
    bust = bust.resize((int(bw * scale), int(bh * scale)), Image.Resampling.NEAREST)
    bw, bh = bust.size

    # 底部渐隐：乘到 alpha 上；不能用 paste（会把背景一起抹成透明）
    from PIL import ImageChops

    fade = Image.new("L", (bw, bh), 255)
    fd = ImageDraw.Draw(fade)
    fade_h = int(bh * 0.20)
    for i in range(fade_h):
        y = bh - fade_h + i
        fd.line([(0, y), (bw, y)], fill=int(255 * (1 - i / fade_h)))
    bust.putalpha(ImageChops.multiply(bust.getchannel("A"), fade))

    pos = ((SIZE - bw) // 2, SIZE - bh + int(bh * 0.10))
    halo(canvas, bust, pos, color=(130, 240, 235), alpha=0.30)
    canvas.alpha_composite(bust, pos)

    # 轻微暗角，让中间更聚焦
    vign = Image.new("L", (SIZE, SIZE), 0)
    vd = ImageDraw.Draw(vign)
    vd.ellipse([-SIZE * 0.15, -SIZE * 0.15, SIZE * 1.15, SIZE * 1.15], fill=140)
    dark = Image.new("RGBA", (SIZE, SIZE), (8, 12, 18, 255))
    dark.putalpha(vign.filter(ImageFilter.GaussianBlur(60)).point(lambda a: 90 - int(a * 0.62)))
    canvas.alpha_composite(dark)
    frame(canvas, color=(140, 230, 230, 130))
    return canvas


VARIANTS = {
    "1": ("正面极简", variant_1),
    "2": ("斜侧轮廓光", variant_2),
    "3": ("正面+字标", variant_3),
    "4": ("半身特写", variant_4),
}


def build(skin_path: str) -> dict[str, Image.Image]:
    skin = prepare_skin(Image.open(skin_path).convert("RGBA"))
    return {key: fn(skin) for key, (_, fn) in VARIANTS.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skin", default=DEFAULT_SKIN)
    ap.add_argument("--variants", action="store_true", help="生成候选图到 .logo_preview/")
    ap.add_argument("--pick", choices=sorted(VARIANTS), help="把某个候选存成 logo.png")
    args = ap.parse_args()

    if not os.path.exists(args.skin):
        raise SystemExit(f"找不到皮肤表：{args.skin}")

    out = build(args.skin)

    if args.pick:
        path = os.path.join(REPO, "logo.png")
        out[args.pick].save(path)
        print(f"logo.png <- 候选 {args.pick} ({VARIANTS[args.pick][0]})  {os.path.getsize(path)}B")
        return

    os.makedirs(PREVIEW_DIR, exist_ok=True)
    labels = []
    for key, (name, _) in VARIANTS.items():
        p = os.path.join(PREVIEW_DIR, f"logo_v{key}.png")
        out[key].save(p)
        labels.append(f"v{key} {name}")
        print("saved", p, os.path.getsize(p), "B")
    # 拼一张对比图
    keys = sorted(VARIANTS)
    sheet = Image.new("RGBA", (SIZE * len(keys) + 10 * (len(keys) + 1), SIZE + 20), (16, 18, 22, 255))
    for idx, key in enumerate(keys):
        sheet.alpha_composite(out[key], (10 + idx * (SIZE + 10), 10))
    cmp_path = os.path.join(PREVIEW_DIR, "compare.png")
    sheet.save(cmp_path)
    print("saved", cmp_path, "| 候选:", " / ".join(labels))


if __name__ == "__main__":
    main()
