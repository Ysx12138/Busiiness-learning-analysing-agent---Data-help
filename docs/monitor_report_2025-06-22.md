# DataHelp Agent 运行监控报告

日期: 2025-06-22
命令: `.venv/bin/python3 -m datahelp --provider deepseek --mode standard_report`
数据集: RetailStoreProductSalesDataset.csv (15,000 rows, 11 cols)
运行时长: ~5分钟（手动终止时仅完成 4 步）

## 时间线

```
T+0s    启动
T+0s    调用 csv_summary ✓
T+20s   调用 list_files ✓
T+40s   ⚠️ 格式有误，重试中（API 返回了非标准格式）
T+80s   调用 read_file ✓（格式错误后自动恢复成功）
T+120s+ 思考中…（API 调用超时/极慢，持续 3 分钟仍未返回）
```
→ 5 分钟内仅完成 3 次有效工具调用

## 核心问题

### 🔴 问题 1: API 调用极慢（最痛）
- 每次 "思考中…" 等待 15-60 秒
- 一个正常的分析需要 15-25 次 API 调用 → 预计总耗时 **15-30 分钟**
- 根本原因：请求经 Clash 代理（localhost:7890）转发，增加了大量延迟
- 部分调用可能达到 120 秒超时阈值并触发重试，进一步翻倍耗时

### 🟡 问题 2: 格式错误导致重复调用
- 模型偶尔返回非标准格式（`parse_model_output` 返回 "retry"）
- 每次 retry 相当于再等一个完整的 API 周期（20-60 秒）
- 在 4 次 API 调用中已出现 1 次格式错误（25% 概率）

### 🟡 问题 3: 无进度反馈
- 用户看到 "思考中…" 但不知道是在等 API 还是卡住了
- 没有进度百分比或预估剩余时间
- 分不清是正常等待还是死锁

### 🟢 问题 4: Step 0 后台不可运行
- `input()` 在后台进程直接报 `EOFError` 崩溃
- 需要用 `printf '\n\n\n' | ...` 预填输入才能后台运行
- 但这个问题只在后台运行时出现，前台交互时正常

## 各组件耗时估算（从本次测试）

| 阶段 | 预估时间 |
|------|---------|
| 数据探索（csv_summary + list_files + read_file） | ~3 min |
| 写 analysis_01.py | ~2 min |
| 执行 analysis_01.py | ~1 min |
| 写 analysis_02.py（如需） | ~1 min |
| 输出 <final> 报告 | ~5 min |
| 生成 Excel/PDF/HTML | ~1 min |
| **总计** | **~15-20 min** |

## 建议

1. **短期：解决代理延迟**
   - 检查 Clash 代理规则，确认 DeepSeek/Anthropic 流量是否走了直连
   - 或者临时关闭代理运行

2. **中期：增加超时反馈**
   - "思考中…" 旁边显示已等待秒数，或者用旋转动画提示存活状态
   - API 调用超时后自动降级（减少 max_tokens 或简化 prompt）

3. **中长期：减少 API 往返次数**
   - 合并步骤（如 csv_summary + list_files 一次完成）
   - 使用更长的 max_tokens，让模型一次性输出更多内容
