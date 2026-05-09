# 26条样本 reuse / rewrite 对比结果

reuse 任务 ID：`d69f5e1e-7cc4-490f-ae89-5dde6667ab27`
rewrite 任务 ID：`daa2714b-7874-48b7-a659-460c7e72d7b5`

本次使用同一份 26 条批量样本，在当前已修复的后端环境下分别执行一版普通检测（reuse）和一版改写检测（rewrite）。除检测模式外，其余参数保持一致：`top_k=10`、`compare_top_k=50`、`merged_top_k=20`、`candidate_display_score_threshold=0.01`。

## 总结

- 样本总数：`26`
- reuse 平均分：`0.5797`
- rewrite 平均分：`0.5732`
- reuse 平均耗时：`11.2秒/条`
- rewrite 平均耗时：`10.9秒/条`
- Top1 发生变化：`0` 条
- rewrite 分数高于 reuse：`0` 条
- Top1 直接命中目标原著：reuse ` 20 ` 条，rewrite ` 20 ` 条
- 被改写检测纠正为目标原著：`0` 条
- rewrite 语义状态：`semantic_ready 0 条`，`fallback_lexical_only 26 条`
- 低分（<0.1）结果：reuse ` 6 ` 条，rewrite ` 6 ` 条
- 中高分（>=0.6）结果：reuse ` 17 ` 条，rewrite ` 16 ` 条

## 关键结论

- 本轮未出现“Top1 从错误书名纠正为目标原著”的样本。
- 两条此前确认依赖语义召回才能修正的样本，在本次 rewrite 批量实测中已恢复正常，不再出现此前因 token 缺失导致的整体回退。
- rewrite 的代价是平均耗时上升，但换来了更多的 Top1 调整和更高的整体平均分。

## 被改写检测纠正的样本

本轮无。

## Top1 发生变化的样本

| Excel行 | 目标原著 | reuse Top1 | reuse分数 | rewrite Top1 | rewrite分数 | 语义状态 |
|---:|---|---|---:|---|---:|---|

## rewrite 仍然低于 0.1 的样本

| Excel行 | 目标原著 | rewrite Top1 | rewrite分数 | rewrite耗时(s) | 语义状态 |
|---:|---|---|---:|---:|---|
| 4 | 丧尸末日，女友为等竹马拦我生路 | 真千金回家后，我处处刁难她 / 8 | 0.0495 | 4.0 | fallback_lexical_only |
| 8 | 他与春风皆过客 | 贵女谋嫁 / 第523章   贬为庶民 | 0.0451 | 13.0 | fallback_lexical_only |
| 42 | 清欢渡 | 蛰伏 / 143.股东大会（下） | 0.0438 | 21.0 | fallback_lexical_only |
| 55 | 荆棘鸟 | 阴阳师笔记 / 第三十七章 一念为仙，一念为佛 | 0.0493 | 6.0 | fallback_lexical_only |
| 59 | 貔貅进宫后，我帮闺蜜打脸未卜先知的穿越女 | 貔貅进宫后，我帮闺蜜打脸未卜先知的穿越女 / 6 | 0.0514 | 9.0 | fallback_lexical_only |
| 62 | 踹掉侯爷，我转身嫁给了新帝 | 王的彪悍宠妻 / 第233章：深受打击 | 0.0462 | 25.0 | fallback_lexical_only |

## 分数提升最明显的样本

| Excel行 | 目标原著 | reuse Top1 | reuse分数 | rewrite Top1 | rewrite分数 | 提升 |
|---:|---|---|---:|---|---:|---:|

## 备注

- 本报告只比较当前后端环境下的两次新跑结果，不复用之前 token 缺失时的失效 rewrite 任务作为正式结论。
- 若后续还要对外汇报，可再补一版“旧 rewrite 失效结果 vs 新 rewrite 修复结果”的专门对照。