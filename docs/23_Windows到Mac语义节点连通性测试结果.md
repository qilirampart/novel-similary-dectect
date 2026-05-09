# Windows 到 Mac 语义节点连通性测试结果

测试时间：

- `2026-04-30`

测试侧：

- `Windows`

目标侧：

- `Mac Studio (M2 / 32GB)`

目标参数：

- `SSH_HOST=192.168.97.154`
- `SSH_USER=a111`
- `Ollama=11434`
- `Qdrant=6333`

## 1. 最终结论

当前问题已经定位清楚：

1. `Windows -> Mac` 的真实 TCP 链路是通的。
2. `Ollama` 和 `Qdrant` 都已经可以从 Windows 侧访问。
3. `SSH` 服务本身也是通的，剩下的问题不是“端口不通”，而是“首次主机指纹确认 + 登录认证”。
4. 之前那份“Mac 侧没有对外开放”的判断需要废弃。
5. 早期异常高度疑似与 `Windows` 侧当时的 `TUN / 路由状态` 有关，而不在 `Mac` 服务启动状态。
6. `a111` 账号密码登录已经验证成功，Windows 侧现在可以真实登录并执行远程命令。
7. 后续补充复测表明：在当前配置下，即使重新开启 `TUN`，新的连接也仍然可以成功建立。

一句话概括：

- 早期“不通”更像是 `TUN / 路由状态 + 受限环境假阴性` 叠加造成的误判。
- 以当前复测结果看，不能简单下结论说“只要开 `TUN` 就一定不通”。

## 2. 为什么前面会出现误判

前面有两层干扰叠在一起：

1. `Windows` 本机开着代理软件的 `TUN` 模式。
2. 早期测试是在当前受限执行环境里做的，出现了假阴性。

这就像：

- 路本来是通的，
- 但中间先被一层“临时交通管制”拦了一下，
- 然后又隔着一层“有局限的观察窗”去看，
- 最后看上去就像前方整条路都断了。

所以这次修正后的口径，必须以“系统级直连测试”与“真实 SSH 登录测试”为准，而不能只看受限环境下的早期结果。

## 3. 修正后的关键测试结果

以下结果均来自关闭 `TUN` 后、脱离受限环境的系统级测试。

### 3.1 SSH 端口

执行命令：

```powershell
Test-NetConnection 192.168.97.154 -Port 22
```

结果：

- `TcpTestSucceeded=True`

结论：

- `22` 端口真实可达
- `SSH` 服务是在线的

补充说明：

- 这次测试里 `PingSucceeded=False`，但 `TcpTestSucceeded=True`
- 这不矛盾，说明目标主机可能不回 `ICMP`，但 `TCP` 连接仍然正常
- 所以这里应以 `TcpTestSucceeded` 为准，而不是以 `ping` 为准

### 3.2 Ollama API

执行命令：

```powershell
curl.exe --connect-timeout 5 http://192.168.97.154:11434/api/version
```

结果：

```json
{"version":"0.22.0"}
```

结论：

- Windows 已经可以直接访问 Mac 上的 `Ollama`
- `11434` 端口真实可用

### 3.3 Qdrant 端口与 API

执行命令：

```powershell
Test-NetConnection 192.168.97.154 -Port 6333
curl.exe --connect-timeout 5 http://192.168.97.154:6333
```

结果：

- `TcpTestSucceeded=True`

```json
{"title":"qdrant - vector search engine","version":"1.17.1","commit":"eabee371fda447974a94d29fbaa675a6a596cc7b"}
```

结论：

- Windows 已经可以直接访问 Mac 上的 `Qdrant`
- `6333` 端口真实可用

### 3.4 SSH 登录探针

执行命令：

```powershell
ssh -vv -o BatchMode=yes -o ConnectTimeout=5 a111@192.168.97.154 exit
```

关键返回：

- `Connection established.`
- `Host key verification failed.`

结论：

- `SSH` 网络链路和服务本身都没有问题
- 当前失败点不是“连不上”，而是“还没有完成首次主机指纹确认”

这和“门已经找到并且已经走到门口了，但门卫先要求你确认这扇门是不是可信的”是一个意思。

### 3.5 SSH 密码登录实测

执行方式：

- 使用 Windows 侧本机 Python `paramiko` 发起真实密码登录
- 登录成功后执行只读命令：

```bash
whoami && hostname && uname -a
```

结果：

```text
LOGIN_OK
a111
localhost
Darwin localhost 23.6.0 Darwin Kernel Version 23.6.0 ... arm64
```

结论：

- `a111` 账号可以通过密码方式从 Windows 侧真实登录到这台 Mac
- 登录后已经可以正常执行远程只读命令
- 到这一步为止，Windows 到 Mac 的 `SSH` 链路已经不是“待验证”，而是“已打通”

### 3.6 重新开启 TUN 后的新连接复测

补充复测方式：

- 在重新开启 `TUN` 后
- 再次按“新建连接”的口径测试 `SSH / Ollama / Qdrant`

结果：

- `22 / 11434 / 6333` 仍然全部可达
- `Ollama` API 仍可正常返回版本信息
- `Qdrant` API 仍可正常返回服务信息
- 新的 SSH 密码登录仍然成功

结论：

- 当前不能把问题简单归因成“开 `TUN` 就会断”
- 更准确的说法是：早期异常发生在当时的 `TUN / 路由状态` 下，但当前配置已可以正常工作

## 4. 现在真实还差什么

### 4.1 SSH 已经打通，后续只需要按常规方式使用

后续在 Windows 终端可以直接使用：

```powershell
ssh a111@192.168.97.154
```

当前已知状态：

- 主机指纹已确认
- 密码登录已验证成功
- 可以继续用于远程执行模型部署、服务重启、日志排查等操作

### 4.2 Windows 侧仍然建议保留“直连优先”的长期方案

虽然当前重新开启 `TUN` 后也能正常访问，但长期仍建议为这类公司内网节点保留直连规则。

长期更稳的方案：

- 在 `Clash / Clash Verge` 里给这台 Mac 配 `DIRECT`
- 或者给公司内网网段配置 `DIRECT`

推荐先从最小范围开始：

```text
IP-CIDR,192.168.97.154/32,DIRECT,no-resolve
```

如果后面会接更多内网机器，再考虑把常见私网网段统一直连：

```text
IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
IP-CIDR,172.16.0.0/12,DIRECT,no-resolve
IP-CIDR,192.168.0.0/16,DIRECT,no-resolve
```

思路很简单：

- 访问公网走代理
- 访问公司内网机器走直连

这样最不容易互相影响。

## 5. 当前建议的使用方式

Windows 侧后续建议按下面的分工使用：

1. `SSH` 负责远程管理 Mac
2. `Ollama HTTP API` 负责向量化调用
3. `Qdrant HTTP API` 负责向量检索调用

也就是：

- 管机器，用 `SSH`
- 调服务，用 `HTTP API`

不要把“远程桌面操作”当成主工作流。

## 6. 建议保留的复测命令

### 6.1 端口复测

```powershell
Test-NetConnection 192.168.97.154 -Port 22
Test-NetConnection 192.168.97.154 -Port 11434
Test-NetConnection 192.168.97.154 -Port 6333
```

### 6.2 API 复测

```powershell
curl.exe http://192.168.97.154:11434/api/version
curl.exe http://192.168.97.154:6333
```

### 6.3 SSH 常规登录

```powershell
ssh a111@192.168.97.154
```

## 7. 本轮修正后的最终判断

本轮结论已经可以明确写死：

- `Mac` 侧语义节点并没有“没开好”
- `Ollama` 对外可访问
- `Qdrant` 对外可访问
- `SSH` 服务对外可访问
- `a111` 账号密码登录已验证成功
- 早期异常更像是 `TUN / 路由状态 + 受限环境假阴性` 叠加造成，而不是 `Mac` 服务本身异常

因此，后续 Windows 侧的正确处理方向不是继续怀疑 Mac 服务，而是：

1. 给这台 Mac 或公司内网段配置 `DIRECT`
2. 后续默认按 `SSH + HTTP API` 模式接入语义节点
3. 用 Windows 侧直接远程管理 Mac 上的向量化服务和检索服务
