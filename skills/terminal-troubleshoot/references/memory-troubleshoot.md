# 内存排查

## 适用场景

- OOM Kill
- 内存持续增长（疑似泄漏）
- 系统可用内存不足
- Swap 使用过高
- 容器 memory limit 接近

## 排查决策树

```
内存问题
├── 容器被 OOMKilled？
│   ├── memory.usage ≈ memory.limit → 容器内存超限
│   │   ├── 正常业务增长 → 调大 limit
│   │   └── 非预期增长 → 内存泄漏
│   └── 节点 OOM → 节点整体内存不足
├── 内存持续增长？
│   ├── RSS 持续增长 → 进程内存泄漏
│   │   ├── Java → jmap -heap / GC 日志
│   │   ├── Go → pprof heap
│   │   └── 通用 → /proc/<PID>/smaps
│   └── Cache/Buffer 增长 → 正常（可回收）
└── 系统内存不足？
    ├── free -h 中 available < 10% → 风险
    └── swap 使用 > 50% → 性能下降
```

## 命令清单

### Phase 1: 内存总览

```bash
# 系统内存概况
free -h

# 内存详细分布
cat /proc/meminfo | grep -E 'MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree'

# 按内存排序的进程 Top 10
ps aux --sort=-%mem | head -12
```

### Phase 2: 容器内存（K8s 场景）

```bash
# 容器内存使用 vs 限制
kubectl -n <NS> exec <POD> -- sh -c '
  usage=$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes)
  limit=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes)
  echo "usage: $((usage/1024/1024))MB  limit: $((limit/1024/1024))MB  ratio: $((usage*100/limit))%"
'

# 容器内存统计详情
kubectl -n <NS> exec <POD> -- cat /sys/fs/cgroup/memory/memory.stat | grep -E 'rss|cache|swap'

# OOM 计数器（容器级）
kubectl -n <NS> exec <POD> -- cat /sys/fs/cgroup/memory/memory.oom_control
```

### Phase 3: 进程内存分析

```bash
# 进程内存映射
cat /proc/<PID>/status | grep -E 'VmSize|VmRSS|VmSwap|Threads'

# 内存映射详情（按大小排序）
cat /proc/<PID>/smaps_rollup 2>/dev/null || cat /proc/<PID>/status | grep Vm

# Java heap
jmap -heap <PID> 2>&1 | head -30

# Go pprof（如果有 pprof 端口）
curl http://localhost:6060/debug/pprof/heap?debug=1 2>&1 | head -50
```

### Phase 4: OOM 历史

```bash
# 系统 OOM 记录
dmesg | grep -i 'oom\|kill' | tail -20

# 特定进程的 OOM
dmesg | grep -i 'oom\|kill' | grep -i <PROCESS_NAME> | tail -10

# journalctl OOM 记录
journalctl -k | grep -i oom | tail -10 2>/dev/null
```

## 判定标准

| 指标 | 阈值 | 含义 |
|------|------|------|
| usage/limit | > 90% | 即将 OOM |
| MemAvailable/MemTotal | < 10% | 系统内存紧张 |
| SwapUsed/SwapTotal | > 50% | 严重性能下降 |
| RSS 持续增长且无回落 | - | 疑似内存泄漏 |
