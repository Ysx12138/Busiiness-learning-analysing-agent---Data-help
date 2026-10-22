"""生成一份可复现的零售教学数据集。

用途：给 DataHelp 提供一份"结构真实、足够脏"的样本，用来做回归对比。
刻意埋进去的问题：
- 缺失值（satisfaction 约 8%，discount 约 3%）
- 重复行（约 1%）
- 极端值（个别 quantity 为 200 的团购单）
- 口径陷阱（discount 是比例，revenue 需要自己算，表里不给）

用法：python examples/make_sample_data.py
输出：examples/sample_sales.csv
"""
from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

SEED = 20260601
ROWS = 2000

REGIONS = ["华东", "华南", "华北", "西南", "华中"]
CHANNELS = ["天猫", "京东", "抖音", "线下门店", "小程序"]
CATEGORIES = {
    "数码": (199, 4999),
    "家居": (49, 899),
    "美妆": (39, 699),
    "食品": (9, 199),
    "服饰": (59, 1299),
}
PRODUCTS = {
    "数码": ["无线耳机", "机械键盘", "显示器", "移动电源", "摄像头"],
    "家居": ["收纳箱", "四件套", "落地灯", "厨房秤", "晾衣架"],
    "美妆": ["精华液", "口红", "面膜", "防晒霜", "洁面乳"],
    "食品": ["坚果礼盒", "咖啡豆", "牛奶", "零食大礼包", "茶叶"],
    "服饰": ["卫衣", "牛仔裤", "羽绒服", "运动鞋", "衬衫"],
}


def build_rows() -> list[dict]:
    rng = random.Random(SEED)
    start = date(2025, 1, 1)
    rows: list[dict] = []
    for i in range(ROWS):
        category = rng.choices(
            list(CATEGORIES), weights=[0.28, 0.22, 0.2, 0.18, 0.12], k=1
        )[0]
        low, high = CATEGORIES[category]
        # 价格分布右偏，贴近真实零售
        unit_price = round(low + (high - low) * rng.random() ** 2, 2)
        # 季节性：年底和 618 前后走高
        day = start + timedelta(days=rng.randint(0, 364))
        seasonal = 1.0
        if day.month in (11, 12):
            seasonal = 1.45
        elif day.month == 6:
            seasonal = 1.3
        quantity = max(1, int(rng.gauss(3 * seasonal, 1.8)))
        if i % 197 == 0:  # 极端值：团购单
            quantity = 200
        discount = rng.choice([0.0, 0.0, 0.05, 0.1, 0.1, 0.15, 0.2, 0.3])
        cost_per_unit = round(unit_price * rng.uniform(0.45, 0.72), 2)
        satisfaction = rng.choices(
            [1, 2, 3, 4, 5], weights=[0.03, 0.06, 0.16, 0.4, 0.35], k=1
        )[0]
        rows.append(
            {
                "order_id": f"SO{20250000 + i:08d}",
                "order_date": day.isoformat(),
                "region": rng.choices(REGIONS, weights=[0.34, 0.22, 0.2, 0.14, 0.1], k=1)[0],
                "channel": rng.choices(CHANNELS, weights=[0.3, 0.25, 0.2, 0.15, 0.1], k=1)[0],
                "category": category,
                "product": rng.choice(PRODUCTS[category]),
                "unit_price": unit_price,
                "quantity": quantity,
                "discount": discount,
                "cost_per_unit": cost_per_unit,
                "customer_id": f"C{rng.randint(1, 900):04d}",
                "satisfaction": satisfaction,
            }
        )

    # 埋缺失值
    for idx in rng.sample(range(ROWS), int(ROWS * 0.08)):
        rows[idx]["satisfaction"] = ""
    for idx in rng.sample(range(ROWS), int(ROWS * 0.03)):
        rows[idx]["discount"] = ""
    # 埋重复行
    for idx in rng.sample(range(ROWS), int(ROWS * 0.01)):
        rows.append(dict(rows[idx]))
    return rows


def main() -> None:
    out = Path(__file__).with_name("sample_sales.csv")
    rows = build_rows()
    fieldnames = list(rows[0].keys())
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows -> {out}")


if __name__ == "__main__":
    main()
