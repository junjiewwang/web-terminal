# 进程 & CPU 排查

## 适用场景

- CPU 使用率持续高
- 系统 load average 异常
- 进程卡死/无响应
- 线程阻塞/死锁
- 僵尸进程

## 排查决策树

```
CPU / 进程异常
├── load average 高？
│   ├── %us 高 → 用户态 CPU 密集
│   │   ├── top -H → 定位热点线程
│   │   └── perf top / strace → 确认调用栈
│   ├── %sy 高 → 内核态密集（系统调用/中断）
│   │   └── perf stat / strace -c → 系统调用统计
│   ├── %wa 高 → IO 等待（见 disk-io）
│   └── %si 高 → 软中断（网络/磁盘中断）
├── 进程状态 D (uninterruptible sleep)？
│   └── IO 阻塞，见 disk-io
├── 僵尸进程 (Z state)？
│   └── 父进程未回收子进程
└── 单进程 CPU 100%？
    └── 死循环 / GC 风暴 / 热点代码
```

## 命令清单

### Phase 1: 全局 CPU 态势

```bash
# 负载 + 运行时间
uptime

# CPU 核数
nproc

# top 快照（批处理模式，1 次采样）
top -bn1 | head -20

# 按 CPU 排序的进程
ps aux --sort=-%cpu | head -15
```

### Phase 2: 定位热点进程/线程

```bash
# 查看特定进程的线程 CPU
top -H -p <PID> -bn1 | head -20

# 进程线程列表
ps -T -p <PID> -o tid,pcpu,state,comm | sort -k2 -rn | head -10

# 进程状态（确认是否 D/Z 状态）
cat /proc/<PID>/status | grep -E 'State|Threads|VmRSS'
```

### Phase 3: CPU Throttling (容器场景)

```bash
# cgroup CPU 限制
cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us
cat /sys/fs/cgroup/cpu/cpu.cfs_period_us
# quota / period = 可用核数（-1 表示无限制）

# throttle 统计
cat /sys/fs/cgroup/cpu/cpu.stat
# nr_throttled / nr_periods = 被节流比例
# throttled_time = 累计被节流纳秒
```

### Phase 4: 调用栈分析（Java/Go）

```bash
# Java: jstack
jstack <PID> | grep -A 10 'BLOCKED\|WAITING\|TIMED_WAITING' | head -50

# Go: pprof（如果应用暴露了 pprof 端口）
curl http://localhost:6060/debug/pprof/goroutine?debug=1 | head -100

# 通用：strace 系统调用追踪（5 秒采样）
timeout 5 strace -c -p <PID> 2>&1
```

## 判定标准

| 指标 | 阈值 | 含义 |
|------|------|------|
| load / nproc | > 2 | CPU 过载 |
| %us | > 80% | 用户态 CPU 密集 |
| %wa | > 30% | IO 瓶颈 |
| nr_throttled/nr_periods | > 10% | 容器 CPU 被严重节流 |
| D state 进程数 | > 0 | 有进程被 IO 阻塞 |
