# 网络排查

## 适用场景

- 网络不通 / 连接超时
- DNS 解析失败
- 丢包 / 延迟高
- 端口不可达
- TCP 连接异常（TIME_WAIT 堆积等）

## 排查决策树

```
网络问题
├── 完全不通？
│   ├── ping 不通 → IP 层问题
│   │   ├── 路由检查 → ip route / traceroute
│   │   └── 防火墙 → iptables -L / nftables
│   └── ping 通但端口不通 → 传输层问题
│       ├── telnet/nc 检查端口 → 服务未监听 or 防火墙
│       └── ss -tlnp → 确认服务监听状态
├── 间歇性问题？
│   ├── 丢包 → mtr / ping -c 100 统计
│   ├── 延迟抖动 → traceroute 定位跳数
│   └── 连接重置 → 抓包分析 RST
├── DNS 问题？
│   ├── nslookup / dig → 解析是否正常
│   └── /etc/resolv.conf → DNS 配置
└── TCP 连接问题？
    ├── TIME_WAIT 堆积 → ss -s 统计
    ├── ESTABLISHED 过多 → 连接泄漏
    └── SYN_SENT 堆积 → 对端不响应
```

## 命令清单

### Phase 1: 基础连通性

```bash
# 网络接口状态
ip addr show | grep -E 'state|inet '

# ping 测试（快速 3 包）
ping -c 3 -W 2 <TARGET_IP>

# 端口连通性（无 telnet 时用 /dev/tcp）
timeout 3 bash -c 'echo > /dev/tcp/<IP>/<PORT>' && echo "OK" || echo "FAIL"

# DNS 解析
nslookup <DOMAIN> 2>&1 || dig <DOMAIN> +short
cat /etc/resolv.conf
```

### Phase 2: 路由 & 跳数

```bash
# 路由表
ip route show

# 到目标的路由路径
traceroute -n -w 2 -m 15 <TARGET_IP> 2>&1 || tracepath <TARGET_IP>

# MTR（综合 ping + traceroute）
mtr -r -c 10 <TARGET_IP> 2>&1 || echo "mtr not available"
```

### Phase 3: TCP 连接状态

```bash
# 连接统计总览
ss -s

# 特定端口的连接状态
ss -tnp | grep <PORT> | awk '{print $1}' | sort | uniq -c | sort -rn

# TIME_WAIT 数量
ss -tan state time-wait | wc -l

# ESTABLISHED 数量
ss -tan state established | wc -l

# 监听端口确认
ss -tlnp | grep <PORT>
```

### Phase 4: 抓包（仅在需要时）

```bash
# 快速抓包（10 个包，超时 10 秒）
timeout 10 tcpdump -i any host <IP> and port <PORT> -c 10 -nn 2>&1

# 抓 SYN/RST 分析连接问题
timeout 10 tcpdump -i any "tcp[tcpflags] & (tcp-syn|tcp-rst) != 0" and host <IP> -c 20 -nn 2>&1
```

### K8s 网络专项

```bash
# Pod → Service 连通性
kubectl -n <NS> exec <POD> -- curl -s -o /dev/null -w "%{http_code}" http://<SERVICE>:<PORT>/health

# Service → Endpoint 映射
kubectl -n <NS> get endpoints <SERVICE>

# CoreDNS 状态
kubectl -n kube-system get pod -l k8s-app=kube-dns

# Pod DNS 解析
kubectl -n <NS> exec <POD> -- nslookup <SERVICE>.<NS>.svc.cluster.local
```

## 判定标准

| 指标 | 阈值 | 含义 |
|------|------|------|
| 丢包率 | > 1% | 网络不稳定 |
| RTT | > 100ms (同机房) | 延迟异常 |
| TIME_WAIT | > 10000 | 连接回收过慢 |
| DNS 解析 | > 1s | DNS 性能问题 |
