# Qdrant 本机路径记录

更新时间：2026-05-11

本机 Qdrant 固定位置如下：

- 可执行文件：`E:\AI_Models\Qdrant\node\qdrant.exe`
- 配置文件：`E:\AI_Models\Qdrant\config\config.yaml`
- 存储目录：`E:\AI_Models\Qdrant\storage`
- 快照目录：`E:\AI_Models\Qdrant\snapshots`

当前配置文件里的关键监听参数：

```yaml
storage:
  storage_path: "E:/AI_Models/Qdrant/storage"
service:
  host: 0.0.0.0
  http_port: 6333
```

本机启动后应验证：

1. `qdrant.exe` 进程存在
2. `127.0.0.1:6333` 端口监听
3. `http://127.0.0.1:6333/collections` 可返回 collection 列表
4. 正式 chunk collection `novel_semantic_chunk_embeddings_qwen3_8b_1024_v1` 可正常访问

本次排查结论：

- 机器重启后，Qdrant 进程没有自动起来，但本地 storage 与 collections 数据仍在
- 以后如果再遇到 `rewrite` 回退、`semantic_status=fallback_lexical_only` 或 `127.0.0.1:6333` 不通，优先先检查这个路径
