# SQLite 结构复核结论

更新时间：2026-04-29

## 1. 复核目标

确认当前 `data/novel_similarity.sqlite3` 及 `service/schema_v1.sql`，是否可以直接承接当前两批测试数据：

- `self_short_novels`
- `online_owned_novels`

## 2. 当前结论

结论：**不能直接承接两批数据同时入同一套业务主表**。

不是“字段不够”，而是**主键边界不对**。

## 3. 已核实事实

### 当前库内数据

当前 SQLite 中已有：

- `books=194`
- `chapters=113425`
- `raw_manifest_rows=113425`
- `chapter_contents=463`

说明：

- 当前库内主要还是第一批 `self_short_novels` 的结构化结果。
- 第二批 `online_owned_novels` 还没有正式并入同一套业务主表。

### 两批数据之间的主键重叠

已核实：

- `chapter_id` 重叠：`23`
- `book_ext_id` 重叠：`2`

这意味着：

- `chapters.chapter_id` 不能继续单独做主键
- `books.book_ext_id` 不能继续单独做主键

## 4. 具体问题点

### `books`

当前主键：

- `book_ext_id`

问题：

- 两批数据之间已经出现 `book_ext_id` 重叠
- 如果直接合并写入，存在覆盖或语义混淆风险

### `chapters`

当前主键：

- `chapter_id`

问题：

- 两批数据之间已经出现 `chapter_id` 重叠
- 如果直接合并写入，必然发生主键冲突或覆盖

### `chapter_contents`

当前主键：

- `chapter_id`

问题：

- 依赖 `chapters.chapter_id`
- 一旦 `chapter_id` 不是全局唯一，这里也不能继续单列主键

### `semantic_chunks` / `evidence_windows`

当前外键：

- `chapter_id`

问题：

- 未来切片和检索层也会继承这个主键边界问题

## 5. 可以继续复用的部分

以下表仍然可以延续思路：

- `ingest_batches`
- `raw_manifest_rows`

原因：

- `raw_manifest_rows` 已经有 `batch_id`
- 它本来就是“批次维度的原始清单表”

## 6. 下一步建议

建议后续进入清洗和入库前，先做一版 `schema_v2`，核心调整方向如下：

### 方案方向

- 为业务主表引入 `dataset_key` 或 `batch_id`
- 将主键从“单列自然键”改成“批次维度复合键”或“内部代理键”

### 最低要求

- `books` 不能只用 `book_ext_id` 做主键
- `chapters` 不能只用 `chapter_id` 做主键
- `chapter_contents` 不能只用 `chapter_id` 做主键
- `semantic_chunks` / `evidence_windows` 也要跟随新的章节主键体系

## 7. 当前判断

所以当前 `F1` 的结论不是“库结构可直接用”，而是：

- 现有 `schema_v1` 适合验证单批下载和早期脚本
- 不适合直接承接当前两批测试数据同时入库
- 在开始全量清洗入库前，应先补 `schema_v2`
