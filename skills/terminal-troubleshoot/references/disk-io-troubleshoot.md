# 磁盘 & IO 排查

## 适用场景

- 磁盘空间满
- IO 延迟高 / IO wait 高
- 写入速度慢
- inode 耗尽
- 日志文件膨胀

## 排查决策树

```
磁盘/IO 问题
├── 磁盘满？
│   ├── 大文件定位 → du + find
│   ├── 已删除但未释放 → lsof +D
│   └── inode 耗尽 → df -i
├── IO 延迟高？
│   ├── iostat → 确认哪个设备
│   ├── %util > 90% → 设备饱和
│   ├── await > 10ms (SSD) → 异常
│   └── IO 调度器 → cat /sys/block/<DEV>/queue/scheduler
└── 写入慢？
    ├── 同步写入过多 → 应用层问题
    └── RAID 降级 → 硬件问题
```

## 命令清单

### Phase 1: 磁盘空间

```bash
# 各分区使用率
df -h | grep -v tmpfs

# inode 使用率
df -i | grep -v tmpfs

# 当前目录大小 Top 10
du -sh /* 2>/dev/null | sort -rh | head -10

# 大文件定位（>100MB）
find / -xdev -type f -size +100M -exec ls -lh {} \; 2>/dev/null | sort -k5 -rh | head -10

# 已删除但未释放的文件（进程仍持有 fd）
lsof +L1 2>/dev/null | head -20
```

### Phase 2: IO 性能

```bash
# IO 统计（1 秒采样 3 次）
iostat -xz 1 3 2>&1 || cat /proc/diskstats

# 关注指标：
#   %util: 设备繁忙度（>90% 饱和）
#   await: 平均 IO 等待时间 ms
#   r_await / w_await: 读/写等待
#   avgqu-sz: 平均队列深度

# 哪些进程在做 IO
iotop -b -n1 2>&1 | head -15 || echo "iotop not available"

# /proc 级 IO 统计
cat /proc/<PID>/io 2>/dev/null
```

### Phase 3: 日志膨胀排查

```bash
# 日志目录大小
du -sh /var/log/ /var/log/pods/ /var/log/containers/ 2>/dev/null

# 最大的日志文件
find /var/log -name "*.log" -type f -exec ls -lh {} \; 2>/dev/null | sort -k5 -rh | head -10

# 容器日志大小（K8s）
find /var/log/pods/ -name "*.log" -type f -size +100M 2>/dev/null | head -10
```

## 判定标准

| 指标 | 阈值 | 含义 |
|------|------|------|
| 磁盘使用率 | > 90% | 紧急 |
| inode 使用率 | > 90% | 紧急（无法创建新文件） |
| %util | > 90% | IO 设备饱和 |
| await (SSD) | > 10ms | IO 延迟异常 |
| await (HDD) | > 50ms | IO 延迟异常 |
