# Web Terminal Skills — AI 运维能力手册

> wetty-mcp-terminal 的 MCP 工具能力总览、排障 Snippet 速查和运维场景指南。
> 配合 AI Agent 使用，逐步进化为 **AI 运维专家**。

---

## 1. MCP 工具能力矩阵

### 1.1 连接管理

| 工具 | 功能 | 关键参数 | 典型场景 |
|------|------|----------|----------|
| `list_hosts` | 列出可用 SSH 主机树 | `tag`（可选，按标签过滤） | 查看可操作的服务器列表 |
| `connect_host` | 连接到指定主机 | `host_name`, `backend`（tmux/broker） | 建立 SSH 连接，支持多跳堡垒机 |
| `disconnect` | 断开会话 | `session_id` | 释放连接资源 |
| `get_session_status` | 查询会话状态 | `session_id`（可选） | 检查连接是否存活，列出所有会话 |

### 1.2 命令执行

| 工具 | 功能 | 关键参数 | 典型场景 |
|------|------|----------|----------|
| `run_command` | 执行命令获取输出 | `session_id`, `command`, `timeout` | 大多数运维操作的核心工具 |
| `send_input` | 发送任意输入 | `session_id`, `text` | 交互式菜单选择、密码输入、确认操作 |
| `wait_for_output` | 等待特定输出出现 | `session_id`, `pattern`, `timeout` | expect 风格等待，配合 `send_input` 使用 |
| `read_terminal` | 读取终端屏幕 | `session_id`, `lines` | 不确定终端状态时查看当前内容 |

### 1.3 窗口管理（Tmux 模式）

| 工具 | 功能 | 关键参数 | 典型场景 |
|------|------|----------|----------|
| `list_windows` | 列出 tmux 窗口 | `bastion_name` | 查看堡垒机上已连接的二级主机 |
| `switch_window` | 切换 tmux 窗口 | `bastion_name`, `window_name` | 在不同二级主机间快速切换 |

### 1.4 排障脚本（Snippet 系统）

| 工具 | 功能 | 关键参数 | 典型场景 |
|------|------|----------|----------|
| `list_snippet_domains` | 列出排障领域 | 无 | 查看有哪些排障工具集可用 |
| `load_snippet_domain` | 加载脚本到远端 | `session_id`, `domain_id` | 首次使用前注入脚本函数 |
| `run_snippet_command` | 执行排障命令 | `session_id`, `domain_id`, `command_id`, `params` | 参数化执行排障命令 |

---

## 2. 排障 Snippet 速查

### 2.1 Elasticsearch 🔍

| 命令 | 功能 | 语法示例 |
|------|------|----------|
| `es` | 发送 ES API 请求 | `es /_cluster/health` |
| `esl` | 查询索引最近文档 | `esl my-index 20` |
| `esr` | 时间范围查询 | `esr my-index 5m 10` |
| `esq` | 字段精确查询 | `esq my-index status error 10` |
| `esm` | 查看索引 Mapping | `esm my-index` |
| `esn` | 节点信息 | `esn -s`（资源）/ `esn -r`（角色） |
| `ess` | 分片信息 | `ess -u`（未分配）/ `ess -e`（原因） |
| `esa` | 集群管理 | `esa replica my-index 2` |

### 2.2 Kubernetes ☸️

| 命令 | 功能 | 语法示例 |
|------|------|----------|
| `ki` | 列出容器镜像 | `ki tce apm-nacos` |
| `kic` | 对比两命名空间镜像差异 | `kic tce staging apm` |

### 2.3 MySQL 🐬

| 命令 | 功能 | 语法示例 |
|------|------|----------|
| `my` | 进入 MySQL Shell | `my mydb` |
| `myq` | 执行 SQL | `myq 'SELECT count(*) FROM users' mydb` |
| `myl` | 列出库/表 | `myl mydb` |
| `myps` | 进程列表 | `myps -l`（慢查询）/ `myps -w`（锁） |
| `myt` | 表状态 | `myt mydb users` |
| `mys` | 切换实例 | `mys my-secret tce` |

### 2.4 Redis 🔴

| 命令 | 功能 | 语法示例 |
|------|------|----------|
| `rd` | 执行 Redis 命令 | `rd GET my-key` / `rd INFO` |
| `rds` | 进入 Redis Shell | `rds` |
| `rdi` | 实例信息 | `rdi -m`（内存）/ `rdi -r`（复制） |
| `rdk` | 扫描 Key | `rdk user:* 100` |
| `rdg` | Key 详情 | `rdg my-key` |
| `rdm` | 大 Key 分析 | `rdm * 20` |
| `rdx` | 切换实例 | `rdx redis-secret ns1` |

---

## 3. 运维场景指南

### 3.1 版本排查

**场景**：检查某个服务在 K8s 集群中的镜像版本

```
操作流程：
1. list_hosts(tag="tce")           → 找到目标堡垒机
2. connect_host("目标机器名")       → 建立连接
3. load_snippet_domain("k8s")      → 加载 K8s 脚本
4. run_snippet_command("k8s", "ki", {"namespace": "tce", "filter": "apm"})
                                    → 列出镜像版本
```

**进阶**：对比两个环境版本差异
```
run_snippet_command("k8s", "kic", {"ns1": "tce", "ns2": "staging", "filter": "apm"})
```

### 3.2 资源监控

**场景**：查看 Pod 的 CPU/内存使用情况

```
操作流程：
1. connect_host → 连接到 K8s 集群
2. run_command("kubectl top pod -n tce | grep nacos")
3. run_command("kubectl get sts nacos -n tce -o jsonpath='{.spec.template.spec.containers[0].resources}'")
```

**分析要点**：
- CPU：对比 usage vs limit，看是否需要扩容
- 内存：Java 应用（如 Nacos）通常稳定在 JVM 堆上限，80-85% 属正常
- 如果接近 limit（>90%），需关注 OOM 风险

### 3.3 ES 集群排障

**场景**：集群健康检查 + 未分配分片分析

```
操作流程：
1. connect_host → 连接到 ES 所在节点
2. load_snippet_domain("es")
3. run_snippet_command("es", "es", {"path": "/_cluster/health"})    → 集群健康
4. run_snippet_command("es", "ess", {"option": "-u"})               → 未分配分片
5. run_snippet_command("es", "ess", {"option": "-e"})               → 分配失败原因
6. run_snippet_command("es", "esn", {"option": "-s"})               → 节点资源
```

### 3.4 MySQL 慢查询排查

**场景**：发现慢查询并分析

```
操作流程：
1. connect_host → 连接到 DB 所在节点
2. load_snippet_domain("mysql")
3. run_snippet_command("mysql", "myps", {"option": "-l"})           → 慢查询列表
4. run_snippet_command("mysql", "myps", {"option": "-w"})           → 锁等待
5. run_snippet_command("mysql", "myq", {"sql": "EXPLAIN SELECT ...", "db": "mydb"})
                                                                     → 执行计划分析
```

### 3.5 Redis 内存分析

**场景**：Redis 内存使用过高排查

```
操作流程：
1. connect_host → 连接到 Redis 节点
2. load_snippet_domain("redis")
3. run_snippet_command("redis", "rdi", {"option": "-m"})            → 内存概况
4. run_snippet_command("redis", "rdm", {"pattern": "*", "count": "20"})
                                                                     → Top 20 大 Key
5. run_snippet_command("redis", "rdk", {"pattern": "cache:*"})      → 扫描特定前缀 Key
```

### 3.6 多环境对比

**场景**：对比不同环境的配置或版本差异

```
操作流程（Tmux 模式）：
1. connect_host("环境A", backend="tmux") → 连接环境 A
2. run_command("kubectl get cm -n tce my-config -o yaml")
3. switch_window("环境B")               → 切换到环境 B
4. run_command("kubectl get cm -n tce my-config -o yaml")
5. Agent 自动对比两个输出的差异
```

---

## 4. Snippet 扩展指南

### 4.1 添加新领域

只需两步，零代码改动：

1. **编写 Shell 脚本**：放到 `config/snippets/` 目录
2. **注册到 YAML**：在 `config/snippets.yaml` 添加领域定义

```yaml
- id: nginx
  name: Nginx
  icon: "🌐"
  description: Nginx 排障工具集
  script_file: snippets/nginx-snippet.sh
  default_timeout: 15
  tags: [web, proxy]
  commands:
    - id: ngx
      name: 查看配置
      description: 查看 Nginx 配置
      syntax: "ngx [vhost]"
      template: "ngx {{vhost}}"
      params:
        - name: vhost
          description: 虚拟主机名（留空查所有）
          default: ""
          required: false
```

修改后 **自动热加载**，无需重启。

### 4.2 候选扩展领域

| 领域 | 说明 | 优先级 |
|------|------|--------|
| **Kafka** | Topic 查看、消费组 lag、分区分布 | 高 |
| **Docker** | 容器日志、资源、网络排查 | 高 |
| **Nginx/OpenResty** | 配置检查、access log 分析 | 中 |
| **JVM** | jstat、jstack、heap dump 分析 | 中 |
| **Network** | TCP 连接、DNS、延迟测试 | 中 |
| **Disk** | IO 分析、空间清理、大文件查找 | 低 |

---

## 5. AI 运维进化路线

### Phase 1：工具使用者（当前）

- ✅ 通过 MCP 工具连接远程主机
- ✅ 使用 Snippet 执行标准化排障命令
- ✅ 读取输出并给出分析建议

### Phase 2：经验积累

- 📋 建立运维 Runbook（标准排障手册）
- 📋 常见问题模式识别（如内存泄漏 → JVM 分析 → heap dump）
- 📋 告警关联分析（Nacos 内存高 → 检查实例数 → 检查 GC）

### Phase 3：自主诊断

- 📋 多步骤自动化诊断流程
- 📋 异常自动检测（资源使用趋势、日志异常模式）
- 📋 根因推理链（从表象 → 关联指标 → 定位根因）

### Phase 4：AI 运维专家

- 📋 预测性维护（资源增长趋势预警）
- 📋 自动修复建议（扩容、重启、配置调整）
- 📋 运维知识图谱（组件依赖、故障传播路径）
- 📋 与告警系统集成（自动响应 On-Call 告警）

---

## 6. 使用技巧

### 6.1 会话复用

同一台主机不需要重复连接，`connect_host` 会自动复用已有会话。

### 6.2 Snippet 复用

同一会话中 `load_snippet_domain` 会自动检测是否已加载，重复调用不会重复注入。

### 6.3 超时控制

- `run_command` 默认 30s，大数据量查询请增大 `timeout`
- Snippet 命令有各自的超时配置（命令级 > 领域级 > 全局 30s）
- 对于耗时操作（如 ES 时间范围查询、Redis 大 Key 扫描），已预设更大超时

### 6.4 交互式操作

对于需要交互的场景（如堡垒机菜单），使用 `send_input` + `wait_for_output` 组合：

```
send_input("1\n")                          → 发送菜单选项
wait_for_output("password:", timeout=10)   → 等待密码提示
send_input("mypassword\n")                 → 输入密码
wait_for_output("#", timeout=30)           → 等待 shell 提示符
```

### 6.5 排障原则

1. **先观察后操作**：先 `read_terminal` / `run_command` 了解现状
2. **标准化优先**：能用 Snippet 就用 Snippet，避免手敲复杂命令
3. **资源感知**：执行前考虑命令对目标系统的影响（如 `KEYS *` 可能阻塞 Redis）
4. **保留现场**：排障前先保存关键信息（如 `kubectl describe pod` 输出）
