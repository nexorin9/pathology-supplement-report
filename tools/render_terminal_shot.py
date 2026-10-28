"""把一次真实运行的终端输出渲染成 PNG 截图。

**这是开发辅助工具，不是产品运行期依赖。** 产品运行时零第三方依赖；本脚本
只在「重新采集 docs/screenshots/ 里的实测截图」时用到，需要 Pillow
（``pip install pillow``）。

为什么需要它：本产品的交付形态是「事件驱动守护 + 运维 CLI」，没有图形界面，
实测证据就是**真实终端会话的输出**。与其贴一段代码块，不如把终端里真的出现
过的那屏输出渲染成图片，让读者一眼看到「跑出来长这样」。

渲染要点（保证图片里每一行都是终端里真实出现过的字符）：

- 按**等宽网格**排版：ASCII 用 Menlo，中日韩字符按 2 个字宽占位，
  中文字形用 Hiragino Sans GB —— 中英混排时列不会串位。
- 不做任何"美化"：不改写、不截断语义、不补内容；只做颜色与留白。
- 文本来源是 ``subprocess`` 抓到的 stdout / stderr 原文。

用法::

    python tools/render_terminal_shot.py 终端记录.txt 输出.png --title "xxx"
"""

from __future__ import annotations

import argparse
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

#: 等宽 ASCII 字体（macOS 自带）。
ASCII_FONT_PATH = "/System/Library/Fonts/Menlo.ttc"
#: 中日韩字形字体（macOS 自带）。
CJK_FONT_PATH = "/System/Library/Fonts/Hiragino Sans GB.ttc"

BG = (30, 32, 43)
CHROME = (44, 47, 62)
TEXT = (216, 222, 233)
PROMPT = (166, 227, 161)
LOG_LINE = (137, 180, 250)
MUTED = (114, 121, 140)
DOTS = ((255, 95, 87), (254, 188, 46), (40, 200, 64))


def _char_cells(ch: str) -> int:
    """一个字符占几个等宽字位（全角 2、半角 1、组合符 0）。"""
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _is_cjk(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F")


def render(
    lines: list[str],
    out_path: Path,
    *,
    title: str = "",
    font_size: int = 16,
    max_cols: int = 118,
) -> Path:
    """把若干行终端文本渲染成一张终端窗口样式的 PNG。"""
    ascii_font = ImageFont.truetype(ASCII_FONT_PATH, font_size)
    cjk_font = ImageFont.truetype(CJK_FONT_PATH, font_size)
    cell_w = ascii_font.getlength("M")
    line_h = round(font_size * 1.55)

    # 超过可视宽度的行按终端方式折行 —— 不截断内容（截图里的每一行都是
    # 终端里真实出现过的字符，宁可多一行也不悄悄丢掉后半句）。
    trimmed: list[str] = []
    for line in lines:
        cells = 0
        current: list[str] = []
        for ch in line:
            w = _char_cells(ch)
            if cells + w > max_cols and current:
                trimmed.append("".join(current))
                current = []
                cells = 0
            current.append(ch)
            cells += w
        trimmed.append("".join(current))

    pad = 22
    bar_h = round(font_size * 1.9)
    width = round(cell_w * max_cols) + pad * 2
    height = bar_h + pad * 2 + line_h * max(len(trimmed), 1)

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    # 窗口标题栏 + 三个圆点（像真终端，不像设计稿）
    draw.rectangle([0, 0, width, bar_h], fill=CHROME)
    dot_r = round(font_size * 0.32)
    cy = bar_h // 2
    for i, color in enumerate(DOTS):
        cx = pad + i * (dot_r * 2 + round(font_size * 0.45))
        draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=color)
    if title:
        # 标题可能含中文 —— 用 CJK 字形（它同样覆盖 ASCII），避免出现方块字。
        tf = ImageFont.truetype(CJK_FONT_PATH, round(font_size * 0.85))
        tw = tf.getlength(title)
        draw.text(((width - tw) / 2, cy - round(font_size * 0.5)), title, font=tf, fill=MUTED)

    def draw_line(text: str, y_pos: int, color: tuple[int, int, int], x0: int) -> None:
        """按等宽网格逐字绘制，返回下一个字的横坐标。"""
        x = x0
        for ch in text:
            font = cjk_font if _is_cjk(ch) else ascii_font
            draw.text((x, y_pos), ch, font=font, fill=color)
            x += cell_w * _char_cells(ch)
        return x

    y = bar_h + pad
    for line in trimmed:
        if line.startswith("$ "):
            draw.text((pad, y), "$", font=ascii_font, fill=PROMPT)
            draw_line(line[2:], y, PROMPT, pad + cell_w * 2)
        elif line.startswith("event="):
            draw_line(line, y, LOG_LINE, pad)
        else:
            draw_line(line, y, TEXT, pad)
        y += line_h

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把终端记录渲染成 PNG 截图")
    parser.add_argument("source", help="终端记录文件（utf-8）")
    parser.add_argument("out", help="输出 PNG 路径")
    parser.add_argument("--title", default="", help="窗口标题")
    parser.add_argument("--font-size", type=int, default=16)
    parser.add_argument("--max-cols", type=int, default=118)
    args = parser.parse_args(argv)

    text = Path(args.source).read_text(encoding="utf-8")
    lines = text.rstrip("\n").split("\n")
    path = render(
        lines,
        Path(args.out),
        title=args.title,
        font_size=args.font_size,
        max_cols=args.max_cols,
    )
    print(f"已生成截图：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
