# rewrite 模式实战 Smoke 验证结果

更新时间：2026-05-02

## 1. 这次验证想回答什么

这次不是再跑单条 demo，也不是只看语义召回脚本本身。

这次要验证的是：

- 当前 `rewrite` 模式是否已经能稳定走通
- 是否真的接入了语义召回，而不是假装开关
- 在当前 `smoke_500` 语义集合上，整条链路的命中情况怎么样
- 本机 `Ollama + Qdrant` 能不能作为当前主验证节点

一句话说，就是验证：

- `rewrite = 词面粗召回 + 语义粗召回 + 候选融合 + 现切 + 细比对`

这条链路现在到底是不是可用状态。

## 2. 验证环境

数据库：

- `data/novel_similarity_v2.sqlite3`

语义服务：

- `Ollama`
  - `http://127.0.0.1:11434`
- `Qdrant`
  - `http://127.0.0.1:6333`

模型：

- `qwen3-embedding:8b`

本次使用的 Qdrant 集合：

- `novel_chapter_embeddings_smoke_500`
  - `500` 条 chapter 向量
- `novel_semantic_chunk_embeddings_smoke_500`
  - `2663` 条 semantic chunk 向量

说明：

- 这两套集合和前面 `smoke_500` 语义评估口径严格一致
- 不存在“query 用前 500 章，向量却跑了别的范围”这种口径错位

## 3. 验证样本

使用 query 集：

- `data_samples/retrieval_eval/self_short_eval_queries_uid500_q300_canonical_v1.csv`

样本规模：

- `50` 条

样本特点：

- 都来自前 `500` 章范围
- 都是 canonical 映射后的正样本
- query 长度为 `300` 字

为什么用这套样本：

- 这是当前更贴近 `rewrite / semantic` 评估口径的样本集
- 比 `120` 字短 query 更能代表后续“剧情摘要 / 短剧文案”式输入

## 4. 验证脚本

新增批量验证脚本：

- `scripts/evaluate_rewrite_smoke_v1.py`

它做的事情是：

1. 逐条读取 query
2. 调 `scripts/run_global_compare_v1.py --detection-mode rewrite`
3. 强制走当前本机语义节点
4. 记录每条 query 的：
   - `semantic_status`
   - `semantic_candidate_count`
   - `self_rank`
   - `top1_review_label`
   - `top1_exact_substring_hit`
5. 最后汇总整批样本结果

这次输出结果文件：

- 汇总：
  - `data_samples/retrieval_eval/rewrite_smoke500_q300_summary_v1.json`
- 明细：
  - `data_samples/retrieval_eval/rewrite_smoke500_q300_eval_v1.csv`

过程中每条 query 的临时结果：

- `data_samples/retrieval_eval/_tmp_rewrite_smoke_v1/`

## 5. 本次结果

### 5.1 链路可用性

- `total_queries = 50`
- `semantic_ready_count = 50`
- `fallback_lexical_only_count = 0`

结论：

- 本次 50 条 query 全部真正接入了语义召回
- 没有任何一条退回成“仅词面链路”

这说明：

- 当前本机 `Ollama + Qdrant` 节点是稳定可用的
- `rewrite` 已经不是“有开关但经常回退”的状态

### 5.2 命中结果

- `Hit@1 = 1.00`
- `Hit@3 = 1.00`
- `Hit@5 = 1.00`
- `Hit@10 = 1.00`
- `MRR = 1.00`
- `missing_target_count = 0`

结论：

- 在这套 `q300 + smoke_500` 样本上，当前 `rewrite` 全链路已实现 `50/50` 的目标章 rank1 命中

### 5.3 结果性质

- `top1_strong_evidence_ratio = 1.00`
- `top1_exact_substring_ratio = 0.00`

这两个指标放在一起看很重要。

它说明：

- 当前 top1 结果虽然不一定是“整段原样直接子串命中”
- 但通过候选现切和细比对后，仍然全部被判成了 `强证据`

换句话说：

- 这次不是单纯靠“原文完全照抄”才拿到高分
- 而是语义召回把对的章节先带回来，后面的 compare 还能稳定确认

### 5.4 候选规模

语义候选数统计：

- `min = 8`
- `p50 = 9`
- `p95 = 10`
- `max = 10`

可疑结果数统计：

- `min = 5`
- `p50 = 5`
- `p95 = 5`
- `max = 5`

细比对候选规模：

- `fine_compared_candidate_count` 全部为 `10`

说明：

- 当前 `merged_top_k=10 / compare_top_k=10 / top_k=5` 这套 smoke 参数是稳定工作的
- 语义侧能稳定补足候选池，不是偶发命中

## 6. 这次验证成立的结论

当前可以明确成立的结论有四条。

### 6.1 `rewrite` 模式已是可运行链路

不是占位，不是半接入，而是：

- 真正接上语义召回
- 真正参与候选融合
- 真正进入后续 compare

### 6.2 Windows 本机已能承担主验证节点角色

这次整轮验证全部使用：

- 本机 `Ollama`
- 本机 `Qdrant`

而且 50 条 query 全部 `semantic_ready`。

所以当前不需要再把“是否能继续推进”建立在远程 Mac 是否在线上。

### 6.3 `q300` 是当前正确的 rewrite smoke 口径

这次结果再次印证：

- 对 `rewrite` 而言，`300` 字 query 比 `120` 字 query 更合理

它更像后续真实输入，也更能发挥语义链路的真实能力。

### 6.4 当前方案具备继续扩样本验证的价值

因为这次不是单条命中，而是：

- 50 条批量跑通
- 结果高度一致
- 没有 fallback

这说明当前 `rewrite` 模式已经值得继续做下一层验证，而不是还停留在“基础设施没打通”阶段。

## 7. 这次结果不代表什么

也要把边界讲清楚，避免误读。

### 7.1 这不是最终洗稿识别准确率

本次样本仍然是：

- 正样本自检
- query 来自小说正文

它验证的是：

- 当前链路能不能把“已知同源章节”稳定找回来

它还没有验证：

- 真实短剧文案输入
- 更强改写样本
- 同剧情不同措辞的人工构造洗稿样本

### 7.2 这不是全库生产压测

本次只对应：

- `smoke_500`

不是：

- 几十万章级语义全量线上压测

所以这次验证的是“路线成立”，不是“生产上限已经证明”。

## 8. 当前最合理的下一步

在这次结果之后，最值得做的不是继续纠结链路通不通，而是往更真实的输入走。

建议下一步按顺序做：

1. 补一批“更像短剧文案”的 query 样本
   - 可以从现有小说正文改写出剧情摘要式 query

2. 补一批“改写但同源”的人工样本
   - 验证 `rewrite` 是否真能扛住洗稿场景

3. 再做一轮 `rewrite` 批量评估
   - 和这次 `q300 smoke500` 结果形成对照

## 9. 当前阶段结论

当前可以正式确认：

- `rewrite` 模式已经具备实战式 smoke 通过条件
- 当前本机语义节点可稳定支撑这条链路
- 当前 `q300` 样本口径下，`rewrite` 全链路在 `smoke_500` 上达到 `50/50 rank1`
- 下一阶段不该再停留在“能不能跑通”，而该进入“更真实样本下还能不能保持效果”
