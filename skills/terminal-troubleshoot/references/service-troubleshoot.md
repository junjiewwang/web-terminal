# 服务异常排查

## 适用场景

- 接口返回错误（5xx / 4xx）
- 请求延迟突增
- 连接拒绝 / 无响应
- 服务间调用失败
- 队列堆积

## 排查决策树

```
服务异常
├── 服务进程存在？
│   ├── 不存在 → 启动失败，查日志
│   └── 存在 → 进入下一步
├── 端口监听？
│   ├── ss -tlnp 无监听 → 进程启动但未 bind
│   └── 有监听 → 进入下一步
├── 能否本地访问？
│   ├── curl localhost:<PORT> 失败 → 应用内部错误
│   └── 本地成功但外部失败 → 网络/LB/Ingress 问题
├── 延迟高？
│   ├── 下游依赖慢 → 追踪调用链
│   ├── 数据库慢查询 → 慢日志
│   ├── GC 停顿 → GC 日志
│   └── 线程池耗尽 → 线程 dump
└── 错误率高？
    ├── 日志中的错误模式 → 聚合分析
    └── 依赖服务不可用 → 级联故障
```

## 命令清单

### Phase 1: 服务存活性

```bash
# 进程确认
ps aux | grep <SERVICE_NAME> | grep -v grep

# 监听端口确认
ss -tlnp | grep <PORT>

# 本地接口测试
curl -s -o /dev/null -w "HTTP %{http_code} Time %{time_total}s\n" http://localhost:<PORT>/health
```

### Phase 2: 应用日志分析

```bash
# 最近错误日志
grep -i 'error\|exception\|fatal\|panic' <LOG_PATH> | tail -20

# 错误频次统计（按类型聚合）
grep -i 'error\|exception' <LOG_PATH> | awk '{print $NF}' | sort | uniq -c | sort -rn | head -10

# K8s 容器日志
kubectl -n <NS> logs <POD> --tail=100 | grep -i 'error\|exception\|panic'

# 时间段内的错误
kubectl -n <NS> logs <POD> --since=5m | grep -ic 'error'
```

### Phase 3: 依赖检查

```bash
# 数据库连通性
timeout 3 bash -c 'echo > /dev/tcp/<DB_HOST>/<DB_PORT>' && echo "DB OK" || echo "DB FAIL"

# Redis 连通性
timeout 3 bash -c 'echo > /dev/tcp/<REDIS_HOST>/6379' && echo "Redis OK" || echo "Redis FAIL"

# K8s Service 端点
kubectl -n <NS> get endpoints <SERVICE_NAME>

# 下游服务健康
curl -s -o /dev/null -w "%{http_code}" http://<DOWNSTREAM>/health
```

### Phase 4: 性能诊断

```bash
# 连接数统计
ss -tnp | grep <PORT> | wc -l

# TCP 状态分布
ss -tnp | grep <PORT> | awk '{print $1}' | sort | uniq -c

# 线程数
cat /proc/<PID>/status | grep Threads

# Java 线程池状态
jstack <PID> 2>&1 | grep -c "java.lang.Thread.State"
jstack <PID> 2>&1 | grep "java.lang.Thread.State" | sort | uniq -c

# Go goroutine 数量
curl http://localhost:<PPROF_PORT>/debug/pprof/goroutine?debug=0 2>&1 | head -1
```

## 判定标准

| 指标 | 阈值 | 含义 |
|------|------|------|
| HTTP 5xx 率 | > 1% | 服务不健康 |
| P99 延迟 | > SLA × 2 | 性能异常 |
| 连接数 | > 预期 × 5 | 连接泄漏嫌疑 |
| 错误日志频率 | 突增 10x | 异常事件 |
