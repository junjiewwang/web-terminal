# K8s Pod 异常排查

## 适用场景

- Pod 频繁重启 (RestartCount > 0)
- CrashLoopBackOff
- OOMKilled
- 健康检查失败 (Liveness/Readiness probe failed)
- ImagePullBackOff
- Pending / ContainerCreating 卡住
- Evicted

## 排查决策树

```
Pod 异常
├── RestartCount 高？
│   ├── Exit Code = 137 → OOMKilled 或 Liveness 超时被杀
│   │   ├── describe pod → Last State → 确认终止原因
│   │   ├── Events 中有 "OOMKilling" → 内存不足，见 [OOM 分支]
│   │   └── Events 中有 "Liveness probe failed" → 健康检查超时，见 [探针分支]
│   ├── Exit Code = 1 → 应用启动失败
│   │   └── logs --previous → 查看上次崩溃前日志
│   └── Exit Code = 143 → SIGTERM（正常关停或抢占）
│       └── Events → 是否有 Preempted/Evicted
├── Pending？
│   ├── Events 有 "Insufficient cpu/memory" → 资源不足
│   ├── Events 有 "node(s) had taint" → 调度约束
│   └── Events 有 "persistentvolumeclaim" → PVC 问题
├── ImagePullBackOff？
│   └── 镜像地址/凭证/网络三连查
└── Running 但 Not Ready？
    └── Readiness probe 持续失败 → 应用启动慢或依赖未就绪
```

## 命令清单

### Phase 1: 状态总览

```bash
# 查看 Pod 状态概览
kubectl -n <NS> get pod <POD> -o wide

# 详细描述（重点看 State / Last State / Events）
kubectl -n <NS> describe pod <POD> | grep -A 30 'State:\|Last State:\|Events:'

# 获取终止状态 JSON（精确的 exitCode 和时间）
kubectl -n <NS> get pod <POD> -o jsonpath='{.status.containerStatuses[0].lastState.terminated}'
```

### Phase 2: 日志分析

```bash
# 当前容器日志（尾部 50 行）
kubectl -n <NS> logs <POD> --tail=50

# 上一次崩溃的日志（--previous）
kubectl -n <NS> logs <POD> --tail=50 --previous

# 多容器 Pod 需指定容器名
kubectl -n <NS> logs <POD> -c <CONTAINER> --tail=50 --previous
```

### Phase 3: 健康检查排查

当 Events 显示 "Liveness/Readiness probe failed" 时：

```bash
# 1. 查看探针配置
kubectl -n <NS> get pod <POD> -o jsonpath='{.spec.containers[0].livenessProbe}' | python3 -m json.tool

# 2. 手动执行健康检查脚本（验证耗时）
kubectl -n <NS> exec <POD> -- sh -c 'time <healthcheck_command>'

# 3. 多次执行确认稳定性
kubectl -n <NS> exec <POD> -- sh -c 'for i in 1 2 3; do time <healthcheck_command>; done'

# 4. 检查 CPU throttling（健康检查慢的常见原因）
kubectl -n <NS> exec <POD> -- cat /sys/fs/cgroup/cpu/cpu.stat
# 关注 nr_throttled / nr_periods 比值

# 5. 检查 CPU cgroup 限制
kubectl -n <NS> exec <POD> -- sh -c 'echo "quota=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us) period=$(cat /sys/fs/cgroup/cpu/cpu.cfs_period_us)"'
# quota/period = 可用 CPU 核数
```

**探针超时判定标准**：
- 脚本基线耗时 > timeout × 0.7 → **高风险**（留余量不足）
- `nr_throttled / nr_periods > 10%` → **CPU 被严重节流**
- 修复：增大 timeoutSeconds / 增大 CPU limit / 优化健康检查脚本

### Phase 4: 资源分析

```bash
# Pod 资源使用
kubectl -n tce top pod | grep <POD_PREFIX>

# 节点资源使用
kubectl top node <NODE_NAME>

# 容器内存详情
kubectl -n <NS> exec <POD> -- cat /sys/fs/cgroup/memory/memory.usage_in_bytes
kubectl -n <NS> exec <POD> -- cat /sys/fs/cgroup/memory/memory.limit_in_bytes

# 容器内进程数
kubectl -n <NS> exec <POD> -- sh -c 'ls /proc/*/exe 2>/dev/null | wc -l'
```

### Phase 5: OOM 排查

```bash
# 1. 确认是否 OOM
kubectl -n <NS> get pod <POD> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}'
# 如果返回 "OOMKilled"

# 2. 节点 dmesg 查看 OOM 记录
dmesg | grep -i 'oom\|kill' | grep -i <SERVICE_NAME> | tail -10

# 3. 当前内存使用 vs 限制
kubectl -n <NS> exec <POD> -- sh -c 'echo "usage=$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes) limit=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes)"'
```

## 常见根因模式

| 现象 | 根因 | 修复方向 |
|------|------|---------|
| Exit 137 + Liveness failed | 健康检查超时 + CPU throttle | 增大 timeout / 增大 CPU limit |
| Exit 137 + OOMKilled | 内存超限 | 增大 memory limit / 排查泄漏 |
| Exit 1 + 启动日志报错 | 配置错误 / 依赖不可用 | 检查 ConfigMap / 依赖服务状态 |
| Running + Not Ready | Readiness 失败 | 检查应用启动依赖 / 注册中心 |
| 高 RestartCount + 短存活时间 | 启动立即崩溃 | logs --previous 查启动异常 |

## 结论输出模板

```markdown
### K8s Pod 重启根因

**Pod**: <namespace>/<pod-name>
**重启次数**: N 次（过去 X 天）
**Exit Code**: 137 (SIGKILL)

**根因**: <具体原因>
**证据**: 
1. ...
2. ...

**修复建议**:
| 优先级 | 措施 | 命令/操作 |
|--------|------|----------|
| P0 | ... | `kubectl patch ...` |
```
