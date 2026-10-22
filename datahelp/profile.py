"""数据集画像 —— 只描述，不下结论。

这一层不调模型、不算业务指标，只回答一个问题：
"这份表长什么样，哪些列可能有用，哪里脏。"

设计要点：
- 不把 order_id 这类主键当成可分组维度（旧引擎在这里翻过车）。
- 日期列按"能解析的比例"判断，而不是看列名。
- 金额/数量这类业务含义，只给候选，不下定论——那是模型该做的判断。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

# 判断主键：唯一值占比高于这个阈值，且行数足够多
_ID_UNIQUE_RATIO = 0.98
_ID_MIN_ROWS = 50
# 判断日期：样本里能解析成日期的比例
_DATE_PARSE_RATIO = 0.8
_DATE_SAMPLE = 200
# 判断分类：唯一值不超过这个数，且不是主键
_MAX_CATEGORICAL = 50

_CURRENCY_HINTS = (
    "price", "revenue", "cost", "amount", "sales", "income", "fee",
    "价", "收入", "成本", "金额", "销售额", "费用",
)
_QUANTITY_HINTS = ("qty", "quantity", "count", "num", "数量", "件数", "销量")
# 比率类：命中这些就先按比例理解，不要被 count 之类的子串带走
_RATIO_HINTS = ("discount", "rate", "ratio", "pct", "percent", "折扣", "率")


@dataclass
class ColumnProfile:
    """单列画像。"""

    name: str
    kind: str  # numeric / datetime / boolean / categorical / text / id / empty
    dtype: str
    non_null: int
    missing: int
    missing_rate: float
    unique: int
    unique_ratio: float
    samples: list[str] = field(default_factory=list)
    min: float | None = None
    max: float | None = None
    mean: float | None = None
    unit_guess: str = ""  # currency / quantity / ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DatasetProfile:
    """整表画像。"""

    path: str
    rows: int
    cols: int
    duplicate_rows: int
    columns: list[ColumnProfile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["columns"] = [c.to_dict() for c in self.columns]
        return d

    @property
    def numeric_columns(self) -> list[ColumnProfile]:
        return [c for c in self.columns if c.kind == "numeric"]

    @property
    def date_columns(self) -> list[ColumnProfile]:
        return [c for c in self.columns if c.kind == "datetime"]

    @property
    def categorical_columns(self) -> list[ColumnProfile]:
        """真正可以分组的列：排除主键、日期、自由文本。"""
        return [c for c in self.columns if c.kind == "categorical"]


def _looks_like_date(series: pd.Series) -> bool:
    sample = series.dropna().astype(str).head(_DATE_SAMPLE)
    if sample.empty:
        return False
    parsed = pd.to_datetime(sample, errors="coerce", format="mixed")
    return parsed.notna().mean() >= _DATE_PARSE_RATIO


def _unit_guess(name: str, kind: str) -> str:
    """按列名猜单位。宁可猜空，也不要猜错——猜错会直接算错指标。

    "discount" 里含 "count"，旧写法会把折扣当成数量，这里先处理比率。
    """
    if kind != "numeric":
        return ""
    lowered = name.lower()
    if any(h in lowered or h in name for h in _RATIO_HINTS):
        return "ratio"
    if any(h in lowered or h in name for h in _CURRENCY_HINTS):
        return "currency"
    if any(h in lowered or h in name for h in _QUANTITY_HINTS):
        return "quantity"
    return ""


def profile_column(series: pd.Series, rows: int) -> ColumnProfile:
    name = str(series.name)
    non_null = int(series.notna().sum())
    missing = int(series.isna().sum())
    unique = int(series.nunique(dropna=True))
    unique_ratio = round(unique / rows, 4) if rows else 0.0
    samples = [str(v) for v in series.dropna().head(3).tolist()]

    if non_null == 0:
        kind = "empty"
    elif pd.api.types.is_bool_dtype(series):
        kind = "boolean"
    elif pd.api.types.is_numeric_dtype(series):
        kind = "numeric"
    elif _looks_like_date(series):
        kind = "datetime"
    elif rows >= _ID_MIN_ROWS and unique_ratio >= _ID_UNIQUE_RATIO:
        # 主键：每行一个值，不能当分组维度
        kind = "id"
    elif unique <= _MAX_CATEGORICAL:
        kind = "categorical"
    else:
        kind = "text"

    prof = ColumnProfile(
        name=name,
        kind=kind,
        dtype=str(series.dtype),
        non_null=non_null,
        missing=missing,
        missing_rate=round(missing / rows, 4) if rows else 0.0,
        unique=unique,
        unique_ratio=unique_ratio,
        samples=samples[:5],
        unit_guess=_unit_guess(name, kind),
    )

    if kind == "numeric":
        numeric = pd.to_numeric(series, errors="coerce")
        prof.min = _round(numeric.min())
        prof.max = _round(numeric.max())
        prof.mean = _round(numeric.mean())

    return prof


def _round(value) -> float | None:
    try:
        if pd.isna(value):
            return None
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def build_profile(csv_path: str) -> DatasetProfile:
    """读取 CSV 并产出画像。读不了就抛错，由调用方决定怎么报。"""
    path = Path(csv_path)
    df = pd.read_csv(path)

    warnings: list[str] = []
    if df.empty:
        warnings.append("数据集没有任何数据行")

    columns = [profile_column(df[col], len(df)) for col in df.columns]

    # 画像层的自检：主键当分类、纯空列、缺失率过高
    for col in columns:
        if col.kind == "empty":
            warnings.append(f"列 {col.name} 全部为空")
        elif col.missing_rate >= 0.5:
            warnings.append(f"列 {col.name} 缺失率 {col.missing_rate:.0%}")
    if not any(c.kind == "numeric" for c in columns):
        warnings.append("没有任何数值列，做不了定量分析")
    if not any(c.kind in ("categorical", "datetime") for c in columns):
        warnings.append("没有可分组或可看趋势的列")

    return DatasetProfile(
        path=str(path.resolve()),
        rows=len(df),
        cols=len(df.columns),
        duplicate_rows=int(df.duplicated().sum()),
        columns=columns,
        warnings=warnings,
    )


def profile_to_text(profile: DatasetProfile) -> str:
    """给模型看的画像文本。列信息完整、可读，不做有损摘要。"""
    lines = [
        f"数据集：{Path(profile.path).name}",
        f"规模：{profile.rows} 行 × {profile.cols} 列；完全重复行 {profile.duplicate_rows} 行",
        "",
        "字段：",
    ]
    for c in profile.columns:
        bits = [f"{c.name}（{c.kind}）"]
        if c.kind == "numeric":
            bits.append(f"范围 {c.min}–{c.max}，均值 {c.mean}")
        if c.kind in ("categorical", "boolean"):
            bits.append(f"{c.unique} 个取值")
        if c.kind == "datetime":
            bits.append("可解析为日期")
        if c.kind == "id":
            bits.append("唯一值几乎等于行数，疑似主键，不宜分组")
        if c.missing:
            bits.append(f"缺失 {c.missing}（{c.missing_rate:.1%}）")
        if c.unit_guess:
            bits.append(f"疑似{c.unit_guess}")
        if c.samples:
            bits.append("示例 " + " / ".join(c.samples[:3]))
        lines.append("  - " + "；".join(bits))
    if profile.warnings:
        lines.append("")
        lines.append("看起来需要注意的地方：")
        lines.extend(f"  - {w}" for w in profile.warnings)
    return "\n".join(lines)
