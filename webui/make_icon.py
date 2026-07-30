"""生成「投流操作台」桌面图标：千川蓝圆角方块 + 白色「投」字。

设计依据 frontend-design：取材业务本身（主色与界面 token --accent #2456e6 一致），
不用模板化图标。输出多尺寸 .ico（16/32/48/256）。

用法：.venv/Scripts/python -m webui.make_icon
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 256
RADIUS = 56
ACCENT = (36, 86, 230)       # --accent 千川蓝
ACCENT_DARK = (23, 58, 166)  # --accent-active，底部压一点深
TEXT = "投"
FONT_PATH = r"C:\Windows\Fonts\msyhbd.ttc"
OUT = Path(__file__).with_name("assets") / "touliu.ico"


def render(size: int) -> Image.Image:
    scale = size / SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 圆角方块：上半主色、下半深色，轻微纵向层次（不用渐变滤镜，直接两段）
    radius = int(RADIUS * scale)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=ACCENT)
    draw.pieslice(
        [-size // 2, int(size * 0.45), int(size * 1.5), int(size * 1.65)],
        start=0, end=180, fill=ACCENT_DARK,
    )
    # 用蒙版把 pieslice 裁回圆角方块内
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    img.putalpha(mask)

    # 白色「投」字居中
    font_size = int(size * 0.62)
    try:
        font = ImageFont.truetype(FONT_PATH, font_size)
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), TEXT, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - w) / 2 - bbox[0]
    y = (size - h) / 2 - bbox[1] - int(size * 0.02)
    draw.text((x, y), TEXT, font=font, fill=(255, 255, 255, 255))
    return img


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    base = render(SIZE)
    base.save(OUT, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    print(f"图标已生成: {OUT}")


if __name__ == "__main__":
    main()
