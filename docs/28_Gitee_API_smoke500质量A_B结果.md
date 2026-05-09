# Gitee API smoke_500 质量 A/B 结果

更新时间：2026-05-02

## 1. 这次 A/B 想回答什么

前面我们已经验证了两件事：

- 本地 `Ollama + qwen3-embedding:8b` 路线，语义链路是能跑通的
- `Gitee API` 路线，在吞吐和稳定性上是可用的

但这还不够。

真正要回答的是：

- 如果把向量来源切到 `Gitee API`
- 当前 `rewrite` 主链路效果会不会明显掉台阶

这次验证，就是在当前 `smoke_500` 口径下，先做一轮最小但真实的质量 A/B。

## 2. 本次验证范围

### 2.1 当前采用的 Gitee 方案

- 模型：`Qwen3-Embedding-8B`
- 维度：`1024`
- API：`https://ai.gitee.com/v1/embeddings`

### 2.2 本次新建集合

本次新建了 Gitee API 版 chunk 集合：

- `novel_semantic_chunk_embeddings_gitee_smoke_500`

规模：

- `2663` 条 `semantic_chunks`
- 范围对应前 `500` 章

说明：

- 本次先只替换 `chunk` 向量来源
- `chapter` 集合仍沿用已有 `novel_chapter_embeddings_smoke_500`

这是有意为之。

因为当前阶段最重要的问题不是“把所有组合一次做满”，而是先判断：

- `Gitee API` 作为 chunk 主向量来源，是否已经足够支撑当前 `rewrite` 主链路

## 3. 本次怎么比

### 3.1 语义召回验证

使用评估集：

- `data_samples/retrieval_eval/self_short_eval_queries_uid500_q300_canonical_v1.csv`

共：

- `50` 条 `q300` query

本次跑的是：

- `remote_embedding_backend = gitee_api`
- `chapter_top_k = 0`
- `chunk_top_k = 8`
- `merged_top_k = 10`

也就是说，这里看的不是“本地双路主线最强口径”，而是：

- 只看 Gitee API 版 `chunk-only` 原始语义召回能力

### 3.2 rewrite 全链路验证

使用同一套 `q300` query：

- `50` 条

本次跑的是：

- `semantic_backend = remote`
- `semantic_remote_embedding_backend = gitee_api`
- `semantic_chapter_collection = novel_chapter_embeddings_smoke_500`
- `semantic_chunk_collection = novel_semantic_chunk_embeddings_gitee_smoke_500`
- `semantic_chapter_top_k = 0`
- `semantic_chunk_top_k = 8`
- `merged_top_k = 10`

这一步看的不是“裸召回”，而是：

- `rewrite = 词面粗召回 + 语义候选 + 现切 + 细比对`

整条链路在 Gitee API chunk 向量下还能不能稳定工作。

## 4. 本次结果

## 4.1 Gitee chunk-only 语义召回结果

结果文件：

- `data_samples/retrieval_eval/self_short_semantic_recall_summary_smoke500_q300_gitee_chunkonly_v1.json`

结果：

- `Hit@1 = 0.86`
- `Hit@3 = 0.88`
- `Hit@5 = 0.88`
- `Hit@10 = 0.88`
- `MRR = 0.87`
- `missing_target_count = 6`

这说明：

- 只用 Gitee API 版 chunk 向量做原始语义召回，是能工作的
- 但它不是“裸召回最强口径”

## 4.2 不能直接和本地 dual-route 口径硬横比

当前本地主线最好结果是：

- `chapter_top_k = 8`
- `chunk_top_k = 8`
- `Hit@1 = 0.98`
- `Hit@10 = 1.00`

那一套是：

- 本地 embedding
- chapter + chunk 双路语义召回

而本次 Gitee 这轮语义召回是：

- Gitee embedding
- 只跑 chunk-only

所以这两者不是严格同口径。

当前更合理的理解方式是：

- Gitee `1024` 的 chunk-only 原始召回，已经在可用区间
- 但不能因为这组裸召回指标比 dual-route 低，就直接判它“不适合项目”

因为我们最终要看的不是“裸召回分数本身”，而是：

- 接到 `rewrite` 全链路后，最终能不能稳定把目标章节打到前面

## 4.3 Gitee rewrite 全链路 smoke 结果

结果文件：

- `data_samples/retrieval_eval/rewrite_smoke500_q300_gitee_chunkonly_summary_v1.json`

结果：

- `semantic_ready_count = 50`
- `fallback_lexical_only_count = 0`
- `Hit@1 = 1.00`
- `Hit@3 = 1.00`
- `Hit@5 = 1.00`
- `Hit@10 = 1.00`
- `MRR = 1.00`
- `missing_target_count = 0`
- `top1_exact_substring_ratio = 0.0`
- `top1_strong_evidence_ratio = 1.0`

这个结果很关键。

它说明：

- 当前 `Gitee API 1024` 版 chunk 向量，接入 `rewrite` 主链路后
- 在 `smoke_500 / q300 / 50条` 这套正式口径下
- 已经可以稳定通过整条链路验证

换句话说：

- 它不是只会“返回一个能看的相似向量”
- 而是真能支撑我们现在的候选召回、细比对和最终排序

## 5. 这次结果怎么解读

## 5.1 当前最重要的正面结论

当前可以正式成立的结论是：

- `Gitee API + Qwen3-Embedding-8B + 1024维`
- 作为当前 `semantic_chunks` 的大规模回填候选主路线
- 已经具备继续推进资格

这里的“具备继续推进资格”，不是一句模糊的“感觉可以”，而是因为：

- 吞吐已经测过
- 批量上限已经摸过
- `rewrite` 全链路 smoke 已经过线

## 5.2 为什么 rewrite 结果比 chunk-only 裸召回更重要

因为我们这个项目最终不是只看：

- “语义召回 Top10 里有没有它”

而是看：

- 最后给人审的结果，是不是把真正同源的章节稳定排到前面

当前 `rewrite` 全链路里，语义召回只是中间一环。

只要它能把正确候选带回来，后面的：

- 候选现切
- 细比对
- 强证据判断

就能继续把结果稳定下来。

所以在工程决策上，这次最重要的不是：

- `chunk-only Hit@10 = 0.88`

而是：

- `rewrite Hit@1 = 1.00`
- `fallback = 0`

## 5.3 当前还不能说什么

这次结果还不能说明：

- `1024` 维一定就是最终最优维度
- `Gitee API` 一定比本地向量更强
- 将来对真实短剧改写样本也一定保持同样效果

当前能说的是：

- 这条 API 路线已经过了第一阶段“质量否决线”
- 没有出现明显掉台阶到不可用的情况

## 6. 当前最合理的工程判断

当前最合理的判断不是“继续纠结 API 和本地谁理论上更好”，而是：

- 本地路线已经完成“验证链路”使命
- Gitee API 路线已经通过“吞吐 + 稳定性 + smoke 质量”三道门
- 所以下一步可以正式进入“大规模回填准备”

## 7. 当前建议

### 7.1 可以正式采用的默认口径

当前建议继续沿用：

- 向量对象：`semantic_chunks(800/200)`
- 模型：`Qwen3-Embedding-8B`
- 维度：`1024`
- 默认回填参数：
  - `batch_size=24`
  - `concurrency=12`

### 7.2 当前不急着做的事

- 暂时不急着补 `chapter` 全量向量
- 暂时不急着做 `200/50 evidence_windows` 全量向量
- 暂时不急着把 `1024/2048/4096` 都做完一整轮大回填再决定

### 7.3 更值得做的下一步

更值得做的是：

1. 把 Gitee API 路线正式接进大规模 chunk 回填脚本
2. 先对当前测试口径下的 canonical chunk 做真实批量回填
3. 补任务状态管理、失败重试和断点续跑
4. 回填完成后再做更完整的检索压测

## 8. 当前阶段结论

这次 A/B 的核心结论可以压缩成一句话：

- `Gitee API 1024维` 在当前 `smoke_500` 口径下，已经通过了 `rewrite` 主链路的质量闸门，可以作为当前大规模 `semantic_chunks` 回填的候选主路线继续推进
