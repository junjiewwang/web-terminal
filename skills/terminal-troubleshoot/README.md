## 技能文档

### 基本信息
- 技能名: `terminal-troubleshoot`
- 创建人: @junjiewwang (junjiewwang@tencent.com)
- 版本: v1.0.0
- 更新时间: 2026-06-04

### 适用场景

通过 `wetty-terminal` MCP 工具连接远程主机，执行系统性运维问题排查：

- K8s Pod 重启 / CrashLoopBackOff / OOMKilled
- 健康检查失败（Liveness/Readiness probe）
- CPU 高负载 / 进程卡死
- 内存泄漏 / OOM
- 磁盘满 / IO 延迟高
- 网络不通 / DNS 失败 / 延迟高
- 服务接口异常 / 延迟突增

### 设计原则

| 原则 | 体现 |
|------|------|
| **高内聚** | SKILL.md 聚焦排查方法论和工具规范，具体场景拆分到独立 reference |
| **低耦合** | 各排查场景（K8s/网络/内存/磁盘/服务）互相独立，按需加载 |
| **可扩展** | 新增排查场景只需添加一个 `references/<场景>.md` 并在路由表注册 |
| **健壮性** | 每个命令有超时控制、权限回退、工具缺失降级方案 |
| **策略模式** | 通过「场景路由表」将问题类型映射到对应的排查策略文档 |

### 架构

```
terminal-troubleshoot/
├── SKILL.md                         # 核心：方法论 + MCP 工具规范 + 安全红线
├── README.md                        # 维护文档（本文件）
└── references/                      # 场景化排查手册（按需加载）
    ├── k8s-pod-troubleshoot.md      # K8s Pod 异常
    ├── process-cpu-troubleshoot.md  # 进程 & CPU
    ├── memory-troubleshoot.md       # 内存
    ├── disk-io-troubleshoot.md      # 磁盘 & IO
    ├── network-troubleshoot.md      # 网络
    └── service-troubleshoot.md      # 服务异常
```

### 前置条件

- 已配置 `wetty-terminal` MCP Server（streamable-http）
- MCP 连接已设置正确的 Bearer Token 认证
- 目标主机已在 wetty-terminal 的 hosts 配置中注册

### 使用示例

```
"帮我排查一下 d12 环境的 tcs-apm-collector Pod 频繁重启的原因"
"xx 主机 CPU 飙到 100%，帮我看看什么进程在占用"
"检查一下生产环境的 Redis 服务为什么连接不上"
"磁盘快满了，帮我看看哪里占用最大"
```

### 扩展指南

新增排查场景步骤：

1. 在 `references/` 下创建新文件 `<场景名>-troubleshoot.md`
2. 文件结构参考现有场景：适用场景 → 决策树 → 命令清单 → 判定标准
3. 在 `SKILL.md` 的「排查场景路由」表中注册新场景和关键词

### 注意事项

⚠️ 所有排查命令均为只读诊断命令，不修改系统状态
⚠️ 修复操作必须先展示方案，获得用户确认后才执行
⚠️ 长命令记得设置 `timeout` 参数，避免 MCP 调用超时
⚠️ 敏感环境操作前确认用户授权

### 已知问题

- [x] K8s Pod 排查完整流程验证（v1.0.0）
- [ ] 添加 Java 性能分析（Arthas）排查场景
- [ ] 添加数据库慢查询排查场景
- [ ] 添加容器镜像/构建问题排查场景

### 相关技能

- `debug`: 代码级调试专家（与本技能互补——本技能关注运维/基础设施层，debug 关注代码逻辑层）
- `api-request-tester`: APM 现网 API 请求测试
