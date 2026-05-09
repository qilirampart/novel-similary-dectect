# Mac 语义节点现状体检与第一阶段落地清单

## 1. 一句话结论

这台 `M2 Max / 32GB Mac Studio` 已经不再是“能不能用”的问题，而是“已经可以作为第一版语义节点正式接入项目”的状态。

当前已经实际打通：

- Windows -> Mac `SSH`
- Windows -> Mac `Ollama /api/embed`
- Windows -> Mac `Qdrant REST API`
- 本地 SQLite -> Mac 端 embedding -> Mac 端 Qdrant 的小样本闭环

当前默认口径已经明确为：

- embedding 模型：`qwen3-embedding:8b`
- 向量维度：`4096`
- 向量库：`Qdrant`

## 2. 当前已验证状态

## 2.1 机器与服务

- 机器：`Apple M2 Max / 32GB / macOS 14.6.1`
- Ollama：`0.22.0`
- Qdrant：`1.17.1`
- 已安装模型：`qwen3-embedding:8b`

## 2.2 网络与远程运维

Windows 侧已经确认可以访问以下端口：

- `22`：SSH
- `11434`：Ollama
- `6333`：Qdrant

这意味着后续可以按下面的角色分工长期使用：

- Windows：主开发机、主业务链路、脚本调度端
- Mac：embedding 节点、Qdrant 节点、后续离线向量化执行节点

## 2.3 正式目录与正式集合

Mac 端已经完成正式语义节点骨架初始化：

- 远程根目录：`/Users/a111/novel_similarity_semantic_node`
- 配置文件：`/Users/a111/novel_similarity_semantic_node/config/node_config.json`

Qdrant 正式集合已经创建：

- `novel_chapter_embeddings`
- `novel_semantic_chunk_embeddings`

两者当前都已按 `4096` 维建立。

## 3. 已经落地到项目代码的能力

Windows 工作区内已经新增并验证：

- `scripts/build_semantic_chunks_v2.py`
  - 作用：按 `800/200` 策略把小说正文切成语义片段并写入本地 `semantic_chunks`
- `scripts/embed_to_qdrant_v1.py`
  - 作用：读取本地章节或语义片段，调用 Mac 上 `qwen3-embedding:8b`，再写入 Mac 上 Qdrant

同时补了一个基础稳态修正：

- `scripts/v2_common.py`
  - SQLite 连接增加 `timeout` 与 `busy_timeout`
- `scripts/build_semantic_chunks_v2.py`
  - 构建时启用 `WAL`

这一步很重要，因为正式库已经比较大，后面你边查库边跑离线任务时，`database is locked` 的概率会明显下降。

## 4. 本轮真实验证结果

## 4.1 本地切片验证

已对正式库做小样本实跑：

- 输入章节数：`20`
- 成功写入 `semantic_chunks`：`49`
- 切片策略：`800 / 200`

说明本地“正文 -> 语义片段”这一步已经可用。

## 4.2 远程向量化与入库验证

已对同一批小样本做真实向量化与入库：

- embedding 模型：`qwen3-embedding:8b`
- embedding 维度：`4096`
- 写入章节向量：`20`
- 写入语义片段向量：`20`

Qdrant 当前实测计数：

- `novel_chapter_embeddings.points_count = 20`
- `novel_semantic_chunk_embeddings.points_count = 20`

这说明下面这条链路已经不是方案层，而是执行层真实可用：

`SQLite -> semantic chunk -> Ollama embed -> Qdrant upsert`

## 5. 当前阶段应如何使用这台 Mac

当前最合理的定位不是把它当“整套项目主机”，而是先把它固定成一台语义节点：

- 提供 embedding API
- 提供向量检索库
- 承担离线小说向量化

不建议第一版就把它再叠成：

- 主业务 API 服务机
- 全部数据库主节点
- 所有链路统一运行机

这样分层更稳，也更符合你们现在“先把洗稿检测支路搭起来”的项目阶段。

## 6. 第一阶段剩余工作

虽然语义节点已经能用了，但还没到“全量生产跑批”。

第一阶段剩余任务应继续按下面顺序推进：

1. 先扩大本地 `semantic_chunks` 构建范围
2. 先做一批更大的章节 / chunk 向量化样本
3. 再补语义召回查询脚本
4. 最后再把 `rewrite` 模式真正接到检索链路上

## 7. 当前明确不再采用的旧口径

下面这些旧判断不再成立：

- “第一版默认先上 `4b`”
- “先补装 `4b`，再把 `8b` 作为对照”
- “当前 Mac 还只是基础设施打通，项目环境没落地”

原因不是理论变化，而是执行结果已经变化：

- 当前 Mac 上实际已安装并验证的是 `8b`
- 正式 collection 已建成
- 小样本真实入库已完成

所以现在的正确说法应该是：

- 当前默认模型就是 `qwen3-embedding:8b`
- 当前语义节点已经进入“可继续扩样和接链路”的阶段

## 8. 当前最重要的判断

当前最重要的判断只有一句：

这台 Mac 现在已经可以作为你们项目第一版 `rewrite / 洗稿检测` 支路的正式语义节点继续往前推进。

后面真正要解决的重点，已经不再是“装服务”，而是：

- 向量化范围
- 召回策略
- `rewrite` 模式接线
- 结果解释与阈值
