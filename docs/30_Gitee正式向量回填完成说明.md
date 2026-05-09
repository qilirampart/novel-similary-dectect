# Gitee 正式向量回填完成说明

## 1. 本次完成内容

本次已完成正式 collection `novel_semantic_chunk_embeddings_qwen3_8b_1024_v1` 的全量 chunk 向量回填。

当前采用的正式口径为：

- 向量模型：`Qwen3-Embedding-8B`
- 向量维度：`1024`
- 数据范围：canonical `semantic_chunks`
- collection：`novel_semantic_chunk_embeddings_qwen3_8b_1024_v1`

## 2. 回填过程摘要

### 2.1 首轮剩余长跑

首轮长跑处理的是正式 collection 在已有 `500,240` 条基础上的剩余部分。

- 启动前累计同步数：`500,240`
- 首轮待回填数量：`1,079,843`
- 运行参数：`batch_size=24`，`concurrency=12`
- 进度文件：`logs/backfill_gitee_chunks_qwen3_8b_1024_v1_remaining_progress.json`

首轮长跑最终结果：

- 状态：`completed_with_failures`
- 处理总量：`1,079,843`
- 成功写入：`1,015,619`
- 失败：`64,224`
- 失败批次：`2,676`
- 耗时：`51,521s`
- 平均吞吐：约 `19.71 向量/秒`

### 2.2 失败尾量补跑

由于回填器基于 `semantic_chunk_embedding_sync_state` 做断点续跑，首轮失败的 `64,224` 条不需要整轮重跑，只需继续扫描尚未成功同步的 chunk。

补跑参数改为更保守配置：

- `batch_size=24`
- `concurrency=8`
- `retry_limit=5`
- `retry_sleep_seconds=3`

补跑进度文件：

- `logs/backfill_gitee_chunks_qwen3_8b_1024_v1_retry_tail_progress.json`

补跑最终结果：

- 状态：`completed`
- 目标量：`64,224`
- 成功：`64,224`
- 失败：`0`
- 耗时：`2,908s`
- 平均吞吐：约 `22.08 向量/秒`

## 3. 最终校验结果

### 3.1 SQLite 校验

最终已做 SQLite 侧等量校验：

- canonical `semantic_chunks` 总量：`1,580,083`
- `novel_semantic_chunk_embeddings_qwen3_8b_1024_v1` 已同步总量：`1,580,083`

两者完全相等，说明当前正式 collection 已无剩余未同步 chunk。

### 3.2 Qdrant 校验

Qdrant collection 状态如下：

- collection：`novel_semantic_chunk_embeddings_qwen3_8b_1024_v1`
- `points_count=1,580,083`
- `status=green`
- `optimizer_status=ok`
- `update_queue.length=0`

这说明 Qdrant 侧也已经完成本轮正式向量数据落库。

## 4. 当前结论

可以将本阶段结论明确为：

- 正式 Gitee chunk 全量回填已完成
- 当前正式语义 collection 已具备全量 canonical chunk 向量
- 本轮不再存在待补的尾量 chunk

## 5. 下一步建议

回填完成后，项目主线应从“补齐向量数据”切换到“验证效果和使用方式”：

- 先用当前正式 collection 跑真实短剧字幕样本，分别验证 `reuse` 与 `rewrite`
- 优先验证那 `69` 条真实待测试数据池中的同名小说
- 对 `rewrite` 模式重点观察“剧情摘要式 / 洗稿式输入”的召回和最终证据桥接效果
- 若后续出现更系统性的漏召回，再考虑补 `chapter` 向量作为增强项，而不是立即扩更多基础设施
