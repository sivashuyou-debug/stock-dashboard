"""スマホのホーム画面用アイコン（チャート風）を作る。  python make_icon.py → icons/"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent / "icons"
S = 1024  # 大きく描いてから縮小してなめらかにする


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw(box=(30, 900, 985, 40)):
    """box = (左, 右, 下, 上) … チャートを描く範囲（1024px 基準）"""
    img = Image.new("RGB", (S, S))
    px = img.load()
    top, bottom = (22, 36, 72), (7, 9, 16)  # 濃紺 → ほぼ黒
    for y in range(S):
        c = lerp(top, bottom, y / S)
        for x in range(S):
            px[x, y] = c
    d = ImageDraw.Draw(img, "RGBA")

    # うっすらしたグリッド
    for i in range(1, 6):
        y = int(S * i / 6)
        d.line([(0, y), (S, y)], fill=(255, 255, 255, 14), width=4)

    # ローソク足（右肩上がり）
    UP, DOWN = (34, 211, 154), (255, 77, 106)
    candles = [  # (始値, 終値, 高値, 安値) ※画面の下=0, 上=1
        (0.30, 0.38, 0.42, 0.26), (0.38, 0.33, 0.41, 0.29), (0.33, 0.47, 0.50, 0.31),
        (0.47, 0.43, 0.52, 0.40), (0.43, 0.58, 0.62, 0.41), (0.58, 0.54, 0.63, 0.50),
        (0.54, 0.72, 0.76, 0.52),
    ]
    left, right, lo_y, hi_y = box
    step = (right - left) / len(candles)
    to_y = lambda v: lo_y - (lo_y - hi_y) * (v - 0.24) / 0.54
    pts = []
    for i, (o, c, h, l) in enumerate(candles):
        cx = left + step * (i + 0.5)
        col = UP if c >= o else DOWN
        d.line([(cx, to_y(h)), (cx, to_y(l))], fill=col + (255,), width=18)
        w = step * 0.34
        d.rounded_rectangle([cx - w, to_y(max(o, c)), cx + w, to_y(min(o, c))], radius=10, fill=col + (255,))
        pts.append((cx, to_y((o + c) / 2 + 0.04)))

    # 光るトレンドライン（青→紫）
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.line(pts, fill=(110, 160, 255, 200), width=46, joint="curve")
    glow = glow.filter(ImageFilter.GaussianBlur(26))
    img.paste(glow, (0, 0), glow)
    line = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ld = ImageDraw.Draw(line)
    for i in range(len(pts) - 1):
        t = i / (len(pts) - 1)
        col = lerp((79, 140, 255), (167, 120, 255), t)
        ld.line([pts[i], pts[i + 1]], fill=col + (255,), width=22, joint="curve")
    # 先端の矢印
    (x1, y1), (x2, y2) = pts[-2], pts[-1]
    ld.line([(x2, y2), (x2 + 70, y2 - 60)], fill=(167, 120, 255, 255), width=22)
    ax, ay = x2 + 70, y2 - 60
    ld.polygon([(ax + 40, ay - 36), (ax - 30, ay - 22), (ax + 22, ay + 30)], fill=(167, 120, 255, 255))
    for x, y in pts:
        ld.ellipse([x - 13, y - 13, x + 13, y + 13], fill=(255, 255, 255, 255))
    img.paste(line, (0, 0), line)
    return img


def main():
    OUT.mkdir(exist_ok=True)
    img = draw()
    for name, size in [("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512), ("favicon-64.png", 64)]:
        img.resize((size, size), Image.LANCZOS).save(OUT / name, optimize=True)
    # Android の丸型などに切り抜かれる用（中央80%に収める）
    draw((150, 830, 860, 230)).resize((512, 512), Image.LANCZOS).save(OUT / "icon-maskable-512.png", optimize=True)
    print("saved", OUT)


if __name__ == "__main__":
    main()
