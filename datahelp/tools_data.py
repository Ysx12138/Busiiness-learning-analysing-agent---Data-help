"""数据分析工具集 —— 数据概览和交付物生成。"""

import csv
import statistics
from pathlib import Path


def read_csv_summary(path: str, repo_root: str) -> str:
    """读取 CSV 文件并返回基础概览：行数、列数、列名。"""
    from datahelp.tools import resolve_path
    path = resolve_path(path, repo_root)
    p = Path(path)
    if not p.exists():
        return f"错误: 文件不存在 {p}"
    if not p.is_file():
        return f"错误: {p} 不是文件"

    try:
        with p.open(newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)
    except Exception as e:
        return f"错误: 无法读取 CSV 文件 - {e}"

    if not rows:
        return f"CSV 文件 {p} 是空的"

    headers = rows[0]
    col_count = len(headers)
    data_rows = len(rows) - 1

    lines = [
        f"文件: {p}",
        f"总行数: {data_rows}",
        f"总列数: {col_count}",
        f"列名: {', '.join(headers)}",
    ]
    return "\n".join(lines)


# ── 交付物生成 ────────────────────────────────────────

def _load_csv_data(path: str, repo_root: str) -> tuple[list[str], list[dict]]:
    """加载 CSV 文件，返回 (列名列表, 数据行列表)。"""
    from datahelp.tools import resolve_path
    path = resolve_path(path, repo_root)
    p = Path(path)
    with p.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return (reader.fieldnames or []), rows


def _infer_numeric_cols(headers: list[str], rows: list[dict]) -> list[str]:
    """推断数值列。"""
    numeric = []
    for col in headers:
        vals = [r.get(col, "").strip() for r in rows if r.get(col, "").strip()]
        if not vals:
            continue
        num_count = sum(1 for v in vals if _is_numeric(v))
        if num_count / len(vals) > 0.9:
            numeric.append(col)
    return numeric


def generate_excel(path: str, repo_root: str, output_dir: str = "", analysis_text: str = "") -> str:
    """生成数据分析 Excel 交付物，包含数据表、统计表、看板和分析报告。"""
    headers, rows = _load_csv_data(path, repo_root)
    import openpyxl
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()

    # ══════════════════════════════════════════════
    # Sheet 1: 原始数据
    # ══════════════════════════════════════════════
    ws_data = wb.active
    ws_data.title = "数据概览"
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    thin_border = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"),
    )

    for c, h in enumerate(headers, 1):
        cell = ws_data.cell(row=1, column=c, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
        cell.border = thin_border

    for r, row in enumerate(rows, 2):
        for c, h in enumerate(headers, 1):
            cell = ws_data.cell(row=r, column=c, value=row.get(h, ""))
            cell.border = thin_border

    for c in range(1, len(headers) + 1):
        ws_data.column_dimensions[get_column_letter(c)].width = max(12, len(headers[c - 1]) + 4)

    # ══════════════════════════════════════════════
    # Sheet 2: 统计摘要
    # ══════════════════════════════════════════════
    ws_stats = wb.create_sheet("统计摘要")
    numeric_cols = _infer_numeric_cols(headers, rows)

    stats_rows = [["指标", "均值", "中位数", "标准差", "最小值", "最大值", "计数"]]
    for col in numeric_cols:
        vals = []
        for r in rows:
            v = r.get(col, "").strip()
            if v:
                try:
                    vals.append(float(v))
                except ValueError:
                    pass
        if len(vals) < 2:
            continue
        vals.sort()
        n = len(vals)
        mean_v = statistics.mean(vals)
        median_v = statistics.median(vals)
        stdev_v = statistics.stdev(vals) if n > 1 else 0.0
        min_v = vals[0]
        max_v = vals[-1]
        stats_rows.append([col, round(mean_v, 2), round(median_v, 2), round(stdev_v, 2), min_v, max_v, n])

    for r, row_data in enumerate(stats_rows, 1):
        for c, val in enumerate(row_data, 1):
            cell = ws_stats.cell(row=r, column=c, value=val)
            if r == 1:
                cell.font = header_font
                cell.fill = header_fill
            cell.border = thin_border

    # 数值列图表
    if len(numeric_cols) >= 1:
        chart = BarChart()
        chart.type = "col"
        chart.title = "数值列对比（均值）"
        chart.y_axis.title = "均值"
        data_ref = Reference(ws_stats, min_col=2, min_row=1, max_row=len(stats_rows), max_col=2)
        cats_ref = Reference(ws_stats, min_col=1, min_row=2, max_row=len(stats_rows))
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)
        chart.width = 20
        chart.height = 12
        ws_stats.add_chart(chart, f"J1")

    for c in range(1, 8):
        ws_stats.column_dimensions[get_column_letter(c)].width = 16

    # ══════════════════════════════════════════════
    # Sheet 3: 分析看板
    # ══════════════════════════════════════════════
    ws_dash = wb.create_sheet("分析看板")

    title_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    title_cell = ws_dash.cell(row=1, column=1, value="数据分析看板")
    title_cell.font = Font(bold=True, size=14, color="FFFFFF")
    title_cell.fill = title_fill
    ws_dash.merge_cells("A1:F1")
    title_cell.alignment = Alignment(horizontal="center")
    for c in range(1, 7):
        ws_dash.cell(row=1, column=c).fill = title_fill

    # KPI 卡片
    kpi_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
    kpi_font = Font(bold=True, size=11)
    row_offset = 3
    ws_dash.cell(row=row_offset, column=1, value="数据集").font = kpi_font
    ws_dash.cell(row=row_offset, column=1).fill = kpi_fill
    ws_dash.cell(row=row_offset, column=2, value=Path(path).name).fill = kpi_fill
    ws_dash.merge_cells(f"B{row_offset}:F{row_offset}")
    ws_dash.cell(row=row_offset + 1, column=1, value="总行数").font = kpi_font
    ws_dash.cell(row=row_offset + 1, column=1).fill = kpi_fill
    ws_dash.cell(row=row_offset + 1, column=2, value=len(rows)).fill = kpi_fill
    ws_dash.cell(row=row_offset + 2, column=1, value="总列数").font = kpi_font
    ws_dash.cell(row=row_offset + 2, column=1).fill = kpi_fill
    ws_dash.cell(row=row_offset + 2, column=2, value=len(headers)).fill = kpi_fill

    if numeric_cols:
        # 前 5 个数值列的关键指标
        row_offset2 = row_offset + 4
        ws_dash.cell(row=row_offset2, column=1, value="关键指标").font = Font(bold=True, size=12)
        for i, col in enumerate(numeric_cols[:5]):
            vals = []
            for r in rows:
                v = r.get(col, "").strip()
                if v:
                    try:
                        vals.append(float(v))
                    except ValueError:
                        pass
            if len(vals) < 2:
                continue
            r = row_offset2 + 1 + i
            ws_dash.cell(row=r, column=1, value=col)
            ws_dash.cell(row=r, column=2, value=f"均值: {statistics.mean(vals):.2f}")
            ws_dash.cell(row=r, column=3, value=f"合计: {sum(vals):.2f}")

    for c in range(1, 7):
        ws_dash.column_dimensions[get_column_letter(c)].width = 18

    # ══════════════════════════════════════════════
    # Sheet 4: 分析报告（agent 分析结论）
    # ══════════════════════════════════════════════
    if analysis_text:
        ws_report = wb.create_sheet("分析报告")
        # 把分析文本按行写入，每行一个单元格
        report_lines = analysis_text.split("\n")
        for r, line in enumerate(report_lines, 1):
            cell = ws_report.cell(row=r, column=1, value=line)
            if r == 1:
                cell.font = Font(bold=True, size=14)
            cell.alignment = Alignment(wrap_text=True)
        ws_report.column_dimensions["A"].width = 120

    # 保存
    output_name = Path(path).stem
    output_path = Path(output_dir) / f"{output_name}_analysis.xlsx" if output_dir else Path(path).parent / f"{output_name}_analysis.xlsx"
    wb.save(str(output_path))
    return f"✅ Excel 已生成: {output_path}"


def _analysis_to_html(text: str) -> str:
    """将 agent 分析报告文本转为 HTML 区块。"""
    import re
    lines = text.strip().split("\n")
    html_parts = ['<h2>分析结论</h2>', '<div class="analysis-content">']
    for line in lines:
        stripped = line.strip()
        if not stripped:
            html_parts.append('<br>')
        elif stripped.startswith("## ") or stripped.startswith("# "):
            level = 2 if stripped.startswith("##") else 3
            title = stripped.lstrip("#").strip()
            html_parts.append(f'<h{level}>{title}</h{level}>')
        elif re.match(r'^\|\s*[-:| ]+\s*\|', stripped):
            continue  # skip markdown table separator rows
        elif stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            html_parts.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        elif stripped.startswith("- "):
            html_parts.append(f'<li>{stripped[2:]}</li>')
        elif re.match(r'^\d+\.', stripped):
            html_parts.append(f'<li>{stripped}</li>')
        elif "**" in stripped:
            html_parts.append(f'<p>{stripped}</p>')
        else:
            html_parts.append(f'<p>{stripped}</p>')
    html_parts.append('</div>')
    return "\n".join(html_parts)


def generate_html(path: str, repo_root: str, output_dir: str = "", analysis_text: str = "") -> str:
    """生成数据分析 HTML 报告，包含统计数据和 agent 分析结论。"""
    headers, rows = _load_csv_data(path, repo_root)
    numeric_cols = _infer_numeric_cols(headers, rows)

    # 统计计算
    stats_html = ""
    for col in numeric_cols:
        vals = []
        for r in rows:
            v = r.get(col, "").strip()
            if v:
                try:
                    vals.append(float(v))
                except ValueError:
                    pass
        if len(vals) < 2:
            continue
        n = len(vals)
        mean_v = statistics.mean(vals)
        stats_html += f"""
        <tr>
            <td>{col}</td>
            <td>{mean_v:.2f}</td>
            <td>{statistics.median(vals):.2f}</td>
            <td>{statistics.stdev(vals):.2f}</td>
            <td>{min(vals):.2f}</td>
            <td>{max(vals):.2f}</td>
            <td>{n}</td>
        </tr>"""

    # 前 5 行数据
    data_rows_html = ""
    for i, row in enumerate(rows[:10]):
        data_rows_html += "<tr>" + "".join(f"<td>{row.get(h, '')}</td>" for h in headers) + "</tr>"

    dataset_name = Path(path).name
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>数据分析报告 - {dataset_name}</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; max-width: 960px; margin: 0 auto; padding: 20px; color: #333; }}
h1 {{ color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }}
h2 {{ color: #2980b9; margin-top: 30px; }}
table {{ border-collapse: collapse; width: 100%; margin: 15px 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
th {{ background-color: #3498db; color: white; font-weight: 600; }}
tr:nth-child(even) {{ background-color: #f8f9fa; }}
.kpi {{ display: inline-block; background: #e8f4f8; border-radius: 8px; padding: 15px 25px; margin: 10px; text-align: center; }}
.kpi-value {{ font-size: 28px; font-weight: bold; color: #2980b9; }}
.kpi-label {{ font-size: 13px; color: #666; }}
.footer {{ margin-top: 40px; padding-top: 20px; border-top: 1px solid #eee; color: #999; font-size: 13px; }}
</style>
</head>
<body>
<h1>数据分析报告</h1>
<p>数据集: <strong>{dataset_name}</strong></p>

<div class="kpi"><div class="kpi-value">{len(rows)}</div><div class="kpi-label">数据行数</div></div>
<div class="kpi"><div class="kpi-value">{len(headers)}</div><div class="kpi-label">字段数量</div></div>
<div class="kpi"><div class="kpi-value">{len(numeric_cols)}</div><div class="kpi-label">数值列</div></div>

<h2>字段信息</h2>
<table><tr>{"".join(f'<th>{h}</th>' for h in headers)}</tr>{data_rows_html}</table>

<h2>描述性统计</h2>
<table>
<tr><th>指标</th><th>均值</th><th>中位数</th><th>标准差</th><th>最小值</th><th>最大值</th><th>计数</th></tr>
{stats_html}
</table>

{_analysis_to_html(analysis_text) if analysis_text else ""}
<div class="footer">
<p>生成工具: DataHelp | 生成时间: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
</div>
</body>
</html>"""

    output_name = Path(path).stem
    output_path = Path(output_dir) / f"{output_name}_report.html" if output_dir else Path(path).parent / f"{output_name}_report.html"
    output_path.write_text(html, encoding="utf-8")
    return f"✅ HTML 已生成: {output_path}"


def generate_pdf(path: str, repo_root: str, output_dir: str = "", analysis_text: str = "") -> str:
    """生成数据分析 PDF 报告，包含 agent 分析结论。"""
    from fpdf import FPDF

    headers, rows = _load_csv_data(path, repo_root)
    numeric_cols = _infer_numeric_cols(headers, rows)
    dataset_name = Path(path).name

    pdf = FPDF()
    pdf.add_page()

    # 封面
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 15, "Data Analysis Report", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(5)
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 10, f"Dataset: {dataset_name}", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 10, f"Rows: {len(rows)}  |  Columns: {len(headers)}", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(10)

    # 关键指标
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "Key Metrics", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 10)
    col_w = pdf.w / (min(len(numeric_cols) + 1, 7))
    pdf.cell(col_w, 8, "Column", 1)
    pdf.cell(col_w, 8, "Mean", 1)
    pdf.cell(col_w, 8, "Median", 1)
    pdf.cell(col_w, 8, "Std", 1)
    pdf.cell(col_w, 8, "Min", 1)
    pdf.cell(col_w, 8, "Max", 1)
    pdf.ln()

    pdf.set_font("Helvetica", "", 9)
    for col in numeric_cols[:8]:
        vals = []
        for r in rows:
            v = r.get(col, "").strip()
            if v:
                try:
                    vals.append(float(v))
                except ValueError:
                    pass
        if len(vals) < 2:
            continue
        n = len(vals)
        pdf.cell(col_w, 7, col[:12], 1)
        pdf.cell(col_w, 7, f"{statistics.mean(vals):.1f}", 1)
        pdf.cell(col_w, 7, f"{statistics.median(vals):.1f}", 1)
        pdf.cell(col_w, 7, f"{statistics.stdev(vals):.1f}", 1)
        pdf.cell(col_w, 7, f"{min(vals):.1f}", 1)
        pdf.cell(col_w, 7, f"{max(vals):.1f}", 1)
        pdf.ln()
        if pdf.get_y() > 260:
            pdf.add_page()

    # 数据概览页（前 20 行）
    if len(rows) > 0:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, "Data Preview", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "B", 7)
        col_w2 = pdf.w / (min(len(headers) + 1, 8))
        for h in headers[:7]:
            pdf.cell(col_w2, 7, h[:10], 1)
        pdf.ln()
        pdf.set_font("Helvetica", "", 7)
        for i, row in enumerate(rows[:25]):
            if pdf.get_y() > 260:
                pdf.add_page()
            for h in headers[:7]:
                pdf.cell(col_w2, 6, str(row.get(h, ""))[:10], 1)
            pdf.ln()

    # 分析报告页
    if analysis_text:
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 15, "Analysis Report", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        for line in analysis_text.split("\n"):
            stripped = line.strip()
            if not stripped:
                pdf.ln(3)
            elif stripped.startswith("##") or stripped.startswith("#"):
                title = stripped.lstrip("#").strip()
                pdf.set_font("Helvetica", "B", 12)
                pdf.multi_cell(0, 8, title)
                pdf.set_font("Helvetica", "", 10)
            elif stripped.startswith("|"):
                # table row → skip in PDF (too complex for fpdf)
                continue
            else:
                pdf.multi_cell(0, 5, stripped)
            if pdf.get_y() > 260:
                pdf.add_page()

    output_name = Path(path).stem
    output_path = Path(output_dir) / f"{output_name}_report.pdf" if output_dir else Path(path).parent / f"{output_name}_report.pdf"
    pdf.output(str(output_path))
    return f"✅ PDF 已生成: {output_path}"
