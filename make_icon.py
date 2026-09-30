"""生成 app.ico —— 蓝色圆角方块 + 白色 "J" 字形，纯标准库实现。

运行一次即可：python make_icon.py
"""
from __future__ import annotations

import struct

# 5x8 点阵字母 J
FONT_J = (
    "00011",
    "00011",
    "00011",
    "00011",
    "00011",
    "11001",
    "11110",
    "01100",
)


def _gradient(t: float):
    """背景纵向渐变：上 #3B82F6 → 下 #1D4ED8。"""
    t = max(0.0, min(1.0, t))
    return (59 + (29 - 59) * t, 130 + (78 - 130) * t, 246 + (216 - 246) * t)


def _coverage(fx: float, fy: float, n: int, radius: float) -> float:
    """圆角矩形的抗锯齿覆盖率（0.0 ~ 1.0），基于有向距离。"""
    px = abs(fx - n / 2) - (n / 2 - radius)
    py = abs(fy - n / 2) - (n / 2 - radius)
    d = (max(px, 0.0) ** 2 + max(py, 0.0) ** 2) ** 0.5 + min(max(px, py), 0.0) - radius
    return max(0.0, min(1.0, 0.5 - d))


def render(size: int) -> bytearray:
    """渲染 size x size 的 RGBA 像素（超采样抗锯齿）。"""
    ss = 4 if size <= 48 else 2
    n = size * ss
    radius = n * 0.16
    cell = (n * 0.60) / len(FONT_J)
    ox = (n - cell * len(FONT_J[0])) / 2
    oy = (n - n * 0.60) / 2

    rgba = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            sr = sg = sb = sa = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    fx = x * ss + sx + 0.5
                    fy = y * ss + sy + 0.5
                    a = _coverage(fx, fy, n, radius)
                    r, g, b = _gradient(fy / n)
                    gx = (fx - ox) / cell
                    gy = (fy - oy) / cell
                    if 0 <= gx < 5 and 0 <= gy < 8 and FONT_J[int(gy)][int(gx)] == "1":
                        r, g, b = 255.0, 255.0, 255.0
                    sr += r * a
                    sg += g * a
                    sb += b * a
                    sa += a
            i = (y * size + x) * 4
            if sa > 0:
                rgba[i] = int(sr / sa)
                rgba[i + 1] = int(sg / sa)
                rgba[i + 2] = int(sb / sa)
            rgba[i + 3] = int(sa / (ss * ss) * 255)
    return rgba


def _bmp_entry(rgba: bytearray, size: int) -> bytes:
    """把 RGBA 编码为 ICO 内嵌的 32 位 BMP（自底向上 BGRA + 全零 AND 掩码）。"""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):
        row = bytearray()
        for x in range(size):
            i = (y * size + x) * 4
            r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    mask_row = b"\x00" * (((size + 31) // 32) * 4)
    return header + b"".join(rows) + mask_row * size


def build_ico(path: str, sizes=(16, 24, 32, 48, 256)) -> None:
    images = [(s, _bmp_entry(render(s), s)) for s in sizes]
    entries = b""
    data = b""
    offset = 6 + 16 * len(images)
    for size, blob in images:
        entries += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset + len(data)
        )
        data += blob
    with open(path, "wb") as handle:
        handle.write(struct.pack("<HHH", 0, 1, len(images)) + entries + data)
    print(f"已生成 {path}（{', '.join(str(s) for s in sizes)} px）")


if __name__ == "__main__":
    build_ico("app.ico")
