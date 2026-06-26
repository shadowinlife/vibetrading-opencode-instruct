# 示例：A股逃顶分析每日预警

以下是 `escape_top_daily` cron 任务每日 08:00 自动执行后的钉钉通知：

---

**OpenCode A股逃顶分析 | 2026-06-24**

🟢 预警级别: GREEN
> 集成模式: VOTE_K_OF_M

条件明细:
> ❌ 两融背离: miss
> ❌ 大单衰竭: miss
> ❌ 波动率ATR扩张: miss
> ✅ 集中度: hit

⚡ 与上次对比: 预警从 RED(06-23) 降为 GREEN；两融背离 hit→miss；大单衰竭 hit→miss

---

**说明**:
- 4 个微观结构条件，VOTE_K_OF_M 集成
- GREEN = 0-1 条件命中，YELLOW = 2 条件命中，RED = 3+ 条件命中
- 每次执行都发送通知（无论是否变化），建立完整历史追踪
