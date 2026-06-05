# PostgreSQL 连接与运维排查

## 适用场景

- 应用连接 PG 失败（密码认证失败 / 数据库不存在 / 连接拒绝）
- PG 慢查询导致应用延迟
- 主从复制延迟或切换异常
- 用户/权限/数据库配置问题
- pg_hba.conf 认证规则异常
- 连接池耗尽

## 排查决策树

```
PostgreSQL 连接失败
├── 网络层可达？
│   ├── 端口不通 → 检查 PG 进程/防火墙/Service/Proxy
│   └── 端口通 → 进入认证层
├── 认证失败 (SQLSTATE 28P01)?
│   ├── pg_hba.conf 有 reject 规则？ → [HBA 排查分支]
│   ├── 密码加密方式不匹配？(SCRAM vs md5) → [加密方式分支]
│   ├── 密码本身不正确？ → [密码验证分支]
│   └── 用户不存在？ → 检查 pg_user
├── 数据库不存在 (SQLSTATE 3D000)?
│   └── 初始化流程未完成 → 手动创建数据库
├── 连接数满 (SQLSTATE 53300)?
│   └── max_connections / 连接泄漏 → [连接池分支]
└── 超时 / 无响应？
    ├── 长事务锁等待
    ├── 复制冲突（standby）
    └── 磁盘 IO 瓶颈
```

## 命令清单

### Phase 1: 网络连通性

```bash
# 1. DNS 解析确认
nslookup <PG_HOST> || dig <PG_HOST>

# 2. 端口连通性（从应用侧测试）
timeout 3 bash -c 'echo > /dev/tcp/<PG_HOST>/<PG_PORT>' && echo "OK" || echo "FAIL"

# 3. K8s 场景：确认 Service/Endpoints
kubectl -n <NS> get svc | grep <PG_SERVICE_NAME>
kubectl -n <NS> get endpoints <PG_SERVICE_NAME>
```

### Phase 2: PG 实例定位（K8s 环境）

> **注意**：不同 K8s 集群的 PG 部署方式差异较大（Operator / Helm / 自研），
> 以下命令需根据实际部署名称调整。核心思路是找到 PG StatefulSet Pod 并进入容器。

```bash
# 1. 找到 PG Pod（按命名模式搜索）
kubectl -n <NS> get pod | grep -i 'postgres\|pg-'

# 2. 确认 Pod 中容器名
kubectl -n <NS> get pod <PG_POD> -o jsonpath='{.spec.containers[*].name}'

# 3. 确认主从角色（通过进程状态）
#    - 主节点进程：postgres (无 "recovering" / "startup" 子进程)
#    - 从节点进程：postgres: startup recovering ...
kubectl -n <NS> exec <PG_POD> -c <CONTAINER> -- sh -c 'ps aux | grep postgres | head -5'

# 4. 查看 PG 数据目录和 socket 位置
kubectl -n <NS> exec <PG_POD> -c <CONTAINER> -- sh -c 'find / -name ".s.PGSQL.*" 2>/dev/null'
```

### Phase 3: 本地 Socket 连接（免密）

> **关键技巧**：大多数 PG 部署的 `pg_hba.conf` 对 `local` 连接配置为 `trust`，
> 这意味着通过 Unix Socket 连接不需要密码。这是排查认证问题时的"后门入口"。

```bash
# 通过 local socket 连接（trust，无需密码）
kubectl -n <NS> exec <PG_POD> -c <CONTAINER> -- sh -c \
  'psql -h <SOCKET_DIR> -U <SUPERUSER> -d postgres -c "<SQL>"'

# 常见 socket 目录：
# /var/lib/postgresql/data
# /var/run/postgresql
# /tmp

# 常见超级用户（按集群部署方式而定）：
# postgres, kw9s0t（TCE athena）, repmgr, patroni
# 如果不确定，通过 ps 查看 PG 主进程的 -U 参数或 pg_user 表

# 查看所有数据库用户
psql -h <SOCKET_DIR> -U <USER> -d postgres -c "SELECT usename, usesuper FROM pg_user;"

# 查看所有数据库
psql -h <SOCKET_DIR> -U <USER> -d postgres -c "SELECT datname FROM pg_database;"
```

### Phase 4: 认证问题排查

#### 4.1 pg_hba.conf 规则检查

```bash
# 查看生效的 HBA 规则（排除注释和空行）
kubectl -n <NS> exec <PG_POD> -c <CONTAINER> -- sh -c \
  'cat <PG_DATA_DIR>/pg_hba.conf | grep -v "^#" | grep -v "^$"'

# 重点关注：
# 1. 是否有 "reject" 规则阻止目标用户
# 2. 规则顺序是否正确（pg_hba.conf 按顺序匹配，第一条匹配即生效）
# 3. 认证方式是否与密码存储方式一致
```

**pg_hba.conf 匹配规则要点**：
| 认证方式 | 含义 | 注意事项 |
|---------|------|---------|
| `trust` | 无条件允许 | 通常只用于 local socket |
| `md5` | MD5 密码验证 | 需要密码以 MD5 格式存储 |
| `scram-sha-256` | SCRAM 验证 | PG 10+，安全性更高 |
| `reject` | 无条件拒绝 | **危险**：放在通用规则前会阻止匹配的连接 |
| `peer` | OS 用户映射 | 仅对 local 连接有效 |

#### 4.2 密码加密方式检查

```bash
# 查看 PG 密码加密设置
psql -h <SOCKET_DIR> -U <USER> -d postgres -c "SHOW password_encryption;"
# 结果：md5 或 scram-sha-256

# 查看用户密码哈希前缀（确认存储格式）
psql -h <SOCKET_DIR> -U <USER> -d postgres -c \
  "SELECT rolname, substring(rolpassword,1,10) as pass_prefix FROM pg_authid WHERE rolname='<TARGET_USER>';"
# "md5" 开头 → MD5 格式
# "SCRAM-SHA-" 开头 → SCRAM 格式
```

**⚠️ 常见陷阱：加密方式不匹配**

| password_encryption | pg_hba.conf 认证 | 结果 |
|--------------------|--------------------|------|
| scram-sha-256 | scram-sha-256 | ✅ 正常 |
| scram-sha-256 | md5 | ❌ **认证失败** |
| md5 | md5 | ✅ 正常 |
| md5 | scram-sha-256 | ❌ **认证失败** |

**修复方式**（二选一）：
```bash
# 方案 A：修改 pg_hba.conf 匹配密码存储方式
sed -i 's/md5/scram-sha-256/g' <PG_DATA_DIR>/pg_hba.conf
psql ... -c "SELECT pg_reload_conf();"

# 方案 B：重新设置密码（以当前 password_encryption 格式重新哈希）
psql ... -c "ALTER USER <USER> PASSWORD '<PASSWORD>';"
```

#### 4.3 密码验证

```bash
# 从 Secret/ConfigMap 获取应用使用的密码
kubectl -n <NS> get secret <SECRET_NAME> -o go-template='{{range $k,$v := .data}}{{$k}}: {{$v | base64decode}}{{"\n"}}{{end}}'

# 在 PG Pod 内通过 TCP（非 local）验证密码
kubectl -n <NS> exec <PG_POD> -c <CONTAINER> -- sh -c \
  'PGPASSWORD=<PASSWORD> psql -h 127.0.0.1 -U <USER> -d <DATABASE> -c "SELECT 1;"'

# ⚠️ 注意：如果 127.0.0.1 在 pg_hba.conf 配置为 trust，
# 则此测试无法验证密码正确性！必须用一个非 trust 的 IP 测试。
```

### Phase 5: 数据库 / 用户 / 权限排查

```bash
# 列出所有数据库
psql ... -c "SELECT datname, datallowconn FROM pg_database;"

# 列出用户及权限
psql ... -c "SELECT usename, usesuper, usecreatedb, usecanlogin FROM pg_user;"

# 检查用户对指定数据库的连接权限
psql ... -c "SELECT datname, has_database_privilege('<USER>', datname, 'CONNECT') FROM pg_database;"

# 创建缺失的数据库
psql ... -c "CREATE DATABASE <DB_NAME> OWNER <USER>;"

# 重新设置用户密码
psql ... -c "ALTER USER <USER> PASSWORD '<PASSWORD>';"
```

### Phase 6: 连接数与性能

```bash
# 当前连接数 vs 最大连接数
psql ... -c "SELECT count(*) as current_conn, (SELECT setting FROM pg_settings WHERE name='max_connections') as max_conn FROM pg_stat_activity;"

# 按状态统计连接
psql ... -c "SELECT state, count(*) FROM pg_stat_activity GROUP BY state;"

# 按客户端 IP 统计
psql ... -c "SELECT client_addr, count(*) FROM pg_stat_activity GROUP BY client_addr ORDER BY count DESC LIMIT 10;"

# 长事务检测
psql ... -c "SELECT pid, now()-xact_start as duration, query FROM pg_stat_activity WHERE state='active' AND now()-xact_start > interval '30 seconds' ORDER BY duration DESC LIMIT 5;"

# 锁等待检测
psql ... -c "SELECT blocked.pid AS blocked_pid, blocked.query AS blocked_query, blocking.pid AS blocking_pid, blocking.query AS blocking_query FROM pg_stat_activity AS blocked JOIN pg_locks bl ON bl.pid = blocked.pid JOIN pg_locks l ON l.locktype = bl.locktype AND l.database IS NOT DISTINCT FROM bl.database AND l.relation IS NOT DISTINCT FROM bl.relation AND l.page IS NOT DISTINCT FROM bl.page AND l.tuple IS NOT DISTINCT FROM bl.tuple AND l.transactionid IS NOT DISTINCT FROM bl.transactionid AND l.classid IS NOT DISTINCT FROM bl.classid AND l.objid IS NOT DISTINCT FROM bl.objid AND l.objsubid IS NOT DISTINCT FROM bl.objsubid AND l.pid != bl.pid JOIN pg_stat_activity AS blocking ON blocking.pid = l.pid WHERE NOT bl.granted LIMIT 5;"
```

### Phase 7: 主从复制状态

```bash
# 确认当前是主还是从
psql ... -c "SELECT pg_is_in_recovery();"
# false = 主节点，true = 从节点

# 主节点：查看复制状态
psql ... -c "SELECT client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn, write_lag, flush_lag, replay_lag FROM pg_stat_replication;"

# 从节点：查看接收延迟
psql ... -c "SELECT now() - pg_last_xact_replay_timestamp() AS replication_delay;"

# WAL 位置差距
psql ... -c "SELECT pg_wal_lsn_diff(sent_lsn, replay_lsn) AS bytes_lag FROM pg_stat_replication;"
```

## 常见根因模式

| 错误 (SQLSTATE) | 现象 | 根因 | 修复方向 |
|----------------|------|------|---------|
| 28P01 | password authentication failed | pg_hba.conf reject / 密码不匹配 / SCRAM vs md5 | 检查 HBA 规则 + 加密方式 |
| 28000 | authentication type not supported | 客户端不支持 SCRAM | 升级客户端库或改 HBA 为 md5 |
| 3D000 | database does not exist | 数据库未创建 | CREATE DATABASE |
| 53300 | too many connections | 连接池泄漏或 max_connections 过小 | 排查连接泄漏 / 调大连接数 |
| 57P03 | the database system is shutting down | PG 正在关停 | 等待重启完成 |
| 08001 | could not connect | 网络不通 / PG 未启动 | 检查进程 + 网络 |
| 40001 | serialization failure | 事务冲突 | 应用层重试 |
| 57014 | query cancelled | 语句超时 | 优化查询或调大 statement_timeout |

## 修复操作 Checklist

> ⚠️ **修复前必须确认**：当前操作的是主节点还是从节点。DDL/DML 只能在主节点执行。

### 修复 pg_hba.conf

```bash
# 1. 编辑 HBA 文件（移除 reject 规则或修改认证方式）
sed -i '/<PATTERN>/d' <PG_DATA_DIR>/pg_hba.conf            # 删除匹配行
sed -i 's/md5/scram-sha-256/g' <PG_DATA_DIR>/pg_hba.conf   # 修改认证方式

# 2. Reload 配置（无需重启）
psql ... -c "SELECT pg_reload_conf();"

# 3. 验证
PGPASSWORD=<PASS> psql -h <HOST> -U <USER> -d <DB> -c "SELECT 1;"

# ⚠️ 如果 PG 由 Operator/Keeper 管理：
# - 修改可能在 Pod 重启时被覆盖
# - 需同时修改 Operator 的配置模板/CRD
# - 多个副本都需要修改
```

### 修复密码

```bash
# 1. 重新设置密码（使用当前 password_encryption 方式重新哈希）
psql ... -c "ALTER USER <USER> PASSWORD '<PASSWORD>';"

# 2. 如果需要从 SCRAM 改为 md5（不推荐，降级安全性）
psql ... -c "SET password_encryption = 'md5'; ALTER USER <USER> PASSWORD '<PASSWORD>';"
```

## 排查经验总结

### 1. 本地 trust ≠ 密码正确

通过 `local trust` 或 `127.0.0.1 trust` 连接成功**不能证明密码正确**。
真正验证密码需要使用非 trust 的连接方式（如从另一个 Pod TCP 连接）。

### 2. PG 15+ 默认 SCRAM

PostgreSQL 14+ 默认 `password_encryption = scram-sha-256`。
如果 pg_hba.conf 仍配置 `md5`，会导致认证失败。

### 3. 多副本必须全部修改

StatefulSet 的每个 Pod 都有独立的 `pg_hba.conf`（持久化在各自 PVC 中）。
修改后需对每个节点执行 sed + reload。

### 4. Operator 覆写风险

由 Operator/Keeper 管理的 PG，`pg_hba.conf` 可能在以下时机被覆写：
- Pod 重启
- 滚动更新
- Failover 切换
需找到 Operator 的 HBA 配置源并同步修改。

### 5. SASL auth failed ≠ 密码错误

错误信息 `failed SASL auth (FATAL: password authentication failed)` 的可能原因：
1. 密码确实错误
2. pg_hba.conf 有 reject 规则（优先级高于密码验证）
3. 加密方式不匹配（SCRAM 存储 + md5 认证）
4. 密码从未被正确初始化（用户创建时未设密码，后续 binding 写入 Secret 的是预期密码但 PG 中未实际设置）
