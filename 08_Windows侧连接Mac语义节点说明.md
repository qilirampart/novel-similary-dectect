# Windows 侧连接 Mac 语义节点说明

更新时间：2026-04-30

## 1. 文档目的

这份文档用于给 `Windows` 侧同事直接参考，说明如何连接当前这台 `Mac` 语义节点，并完成以下三类验证：

- `SSH` 远程登录验证
- `Ollama` embedding 服务验证
- `Qdrant` 向量库服务验证

当前目标不是一次性完成正式联调，而是先确认：

- `Windows -> Mac` 的内网网络是否畅通
- `Mac` 上的语义服务是否可从 `Windows` 访问
- 后续接口开发可以基于哪些连接参数继续推进

## 2. 当前节点信息

当前 `Mac` 语义节点信息如下：

- 节点角色：`Mac` 本地语义节点
- 设备：`Mac Studio (M2 Max / 32GB)`
- 登录用户名：`a111`
- 当前内网 IP：`192.168.97.154`

当前已启动服务如下：

- `SSH`：已在系统设置中开启 `Remote Login`
- `Ollama`：监听 `11434`
- `Qdrant`：监听 `6333`

## 3. 建议的使用方式

建议把连接分成两类，不要混成一种“远控 Mac”：

- 管理这台 `Mac`：使用 `SSH`
- 使用语义能力：调用 `HTTP API`

也就是说：

1. 需要维护机器时，用 `SSH`
2. 需要生成向量或查向量时，用 `HTTP`
3. 不把远程桌面作为主入口

## 4. Windows 侧最小连接参数

建议先在 `Windows` 侧记录以下参数：

```env
SSH_HOST=192.168.97.154
SSH_USER=a111

EMBEDDING_BASE_URL=http://192.168.97.154:11434
QDRANT_URL=http://192.168.97.154:6333
```

如果后面 `Mac` 的内网 IP 变化，需要同步更新这几个参数。

## 5. 第一步：验证 SSH 是否可连

### 5.1 使用 Windows 自带 OpenSSH

在 `PowerShell` 或 `CMD` 中执行：

```bash
ssh a111@192.168.97.154
```

首次连接时如果看到指纹确认提示，输入：

```text
yes
```

然后输入这台 `Mac` 上 `a111` 用户的登录密码。

### 5.2 期望结果

如果连接成功，应进入远程 shell，提示符类似：

```bash
a111@11deMac-Studio ~ %
```

### 5.3 常见失败情况

如果失败，优先检查以下几项：

- `Windows` 和 `Mac` 是否在同一内网
- 公司网络策略是否拦截了 `22` 端口
- `Mac` 上“远程登录”是否确实已开启
- 输入的用户名是否正确

### 5.4 端口连通性测试

如果还没准备好直接 `ssh`，可以先用 `PowerShell` 测试端口：

```powershell
Test-NetConnection 192.168.97.154 -Port 22
```

如果 `TcpTestSucceeded` 为 `True`，说明 `22` 端口可达。

## 6. 第二步：验证 Ollama embedding 服务

### 6.1 验证版本接口

在 `PowerShell` 中执行：

```powershell
curl http://192.168.97.154:11434/api/version
```

期望返回类似：

```json
{"version":"0.22.0"}
```

### 6.2 验证 embedding 接口

在 `PowerShell` 中执行：

```powershell
$body = @{
  model = "qwen3-embedding:8b"
  input = "这是一段中文小说测试文本，用来验证 Windows 到 Mac 的 embedding 调用是否正常。"
} | ConvertTo-Json -Depth 4

Invoke-RestMethod -Uri "http://192.168.97.154:11434/api/embed" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

### 6.3 期望结果

如果调用成功，会返回：

- `model`
- `embeddings`

其中 `embeddings` 里应包含一组浮点向量数组。

### 6.4 当前模型说明

当前 `Mac` 上已安装并验证：

- `qwen3-embedding:8b`

这是当前建议优先使用的主模型。  
后续如有需要，可再补装 `4b` 作为备用模型或对照模型。

## 7. 第三步：验证 Qdrant 服务

### 7.1 验证根接口

在 `PowerShell` 中执行：

```powershell
curl http://192.168.97.154:6333
```

期望返回类似：

```json
{
  "title": "qdrant - vector search engine",
  "version": "1.17.1"
}
```

### 7.2 验证 collection 是否存在

如果后面已经建了 collection，可以进一步测试：

```powershell
curl http://192.168.97.154:6333/collections
```

### 7.3 当前职责说明

当前 `Qdrant` 负责：

- 存储章节向量或语义片段向量
- 提供向量检索能力

`Windows` 侧后面是否直接调用 `Qdrant`，还是改为调用 `Mac` 上再封装的一层统一接口，以接口文档为准。

## 8. 推荐联调顺序

建议按以下顺序联调，不要一开始就混在一起排查：

1. 先测 `22` 端口是否可达
2. 再测 `SSH` 是否能登录
3. 再测 `11434` 是否能拿到版本号
4. 再测 `/api/embed` 是否能返回向量
5. 最后测 `6333` 是否可达

这样能快速定位问题到底在：

- 网络层
- 账号权限
- `Ollama`
- `Qdrant`

## 9. 建议给 Windows 侧的最小测试脚本

如果希望一次性验证三项能力，可在 `PowerShell` 中执行：

```powershell
Test-NetConnection 192.168.97.154 -Port 22
Test-NetConnection 192.168.97.154 -Port 11434
Test-NetConnection 192.168.97.154 -Port 6333
```

如果三个端口都通，再继续：

```powershell
curl http://192.168.97.154:11434/api/version
curl http://192.168.97.154:6333
ssh a111@192.168.97.154
```

## 10. 安全注意事项

当前建议仅在以下场景中使用：

- 公司内网
- 已接入公司 VPN

当前不建议：

- 直接把 `22`
- `11434`
- `6333`

映射到公网开放。

原因很简单：

- `SSH` 对公网暴露会增加攻击面
- `Ollama` 和 `Qdrant` 当前不是按公网服务标准做的安全加固
- 现阶段目标是内部联调，不是公网产品化部署

## 11. 当前结论

对 `Windows` 侧来说，后续接入这台 `Mac` 的方式可以定成：

- 管理机器：`SSH`
- 调用向量能力：`HTTP`

第一版先确认三条链路：

1. `Windows -> SSH -> Mac`
2. `Windows -> Ollama`
3. `Windows -> Qdrant`

三条链路都通以后，再继续接你们自己的业务接口和语义召回逻辑。
