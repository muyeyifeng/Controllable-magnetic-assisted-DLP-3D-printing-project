from PIL import Image

# 图像尺寸
WIDTH, HEIGHT = 1920, 1080

# 小格 10×10
CELL = 100

# 大格 20×20（含 2×2 小格）
BCELL = 2 * CELL

# 要生成的 4 个模式：文件名 → 白块在大格中的 (px, py)
patterns = {
    "pattern_1.png": (0, 0),  # 左上
    "pattern_2.png": (1, 0),  # 右上
    "pattern_3.png": (0, 1),  # 左下
    "pattern_4.png": (1, 1),  # 右下
}


def generate_pattern(filename, pos):
    """生成 1920x1080 的图案，pos=(px,py) 表示白块在大格内的位置"""
    px, py = pos

    # 创建黑底二值图 1 = 白，0 = 黑
    img = Image.new("1", (WIDTH, HEIGHT), 0)

    for by in range(0, HEIGHT, BCELL):    # 每个大格 y
        for bx in range(0, WIDTH, BCELL): # 每个大格 x

            # 白色小块坐标
            x0 = bx + px * CELL
            y0 = by + py * CELL

            # 涂白（小块大小 CELL × CELL）
            for y in range(y0, min(y0 + CELL, HEIGHT)):
                for x in range(x0, min(x0 + CELL, WIDTH)):
                    img.putpixel((x, y), 1)

    img.save(filename)
    print("已保存:", filename)


# 生成全部图片
for name, pos in patterns.items():
    generate_pattern(name, pos)

print("\n全部图片已生成：")
for name in patterns:
    print(name)
