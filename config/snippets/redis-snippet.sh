#!/usr/bin/env bash
# Redis 排查 Snippet — 精简版，粘贴到终端即可用
# 详细帮助: https://git.woa.com/junjiewwang/troubleshoot-snippets/blob/master/redis/README.md

# 从 k8s secret 初始化连接信息并缓存
# Secret 所在 ns 由 RD_NS 控制（默认 tce）
_rd_init() {
  local ns="${RD_NS:-tce}" secret="${RD_SECRET}"
  [ -z "$secret" ] && { echo "RD_SECRET 未设置，请 export RD_SECRET=<secret-name>" >&2; return 1; }
  local yaml
  yaml=$(kubectl get secret -n "$ns" "$secret" -oyaml 2>/dev/null) || { echo "获取 secret 失败: ns=$ns secret=$secret" >&2; return 1; }
  _RD_H=$(echo "$yaml" | grep -oP "(?<=host: ).+" | base64 -d 2>/dev/null)
  _RD_P=$(echo "$yaml" | grep -oP "(?<=port: ).+" | base64 -d 2>/dev/null)
  _RD_U=$(echo "$yaml" | grep -oP "(?<=user: ).+" | base64 -d 2>/dev/null)
  _RD_W=$(echo "$yaml" | grep -oP "(?<=pass: ).+" | base64 -d 2>/dev/null)
  # 密码格式可能是 user:pass 或纯 pass，兼容两种
  _RD_AUTH="${_RD_U:+$_RD_U:}${_RD_W}"
  _RD=1 && echo "Redis: $_RD_H:$_RD_P (secret ns=$ns)" >&2
}

# 构造 redis-cli 命令（优先用 kubectl exec，若设置 RD_POD 则走 exec）
# Pod 所在 ns 优先使用 RD_POD_NS，未设置则回退到 RD_NS（向后兼容）
_rd_cli() {
  local cmd="redis-cli -h $_RD_H -p $_RD_P${_RD_AUTH:+ -a $_RD_AUTH}"
  if [ -n "$RD_POD" ]; then
    local pod_ns="${RD_POD_NS:-${RD_NS:-tce}}" pod="$RD_POD" container="${RD_CONTAINER:-redis}"
    echo "kubectl exec -it $pod -n $pod_ns -c $container -- bash -c \"$cmd $*\""
  else
    echo "$cmd $*"
  fi
}

# 执行 redis 命令
rd() {
  [[ "$1" == "-h" ]] && { echo "rd <cmd...>  # 执行 redis 命令，如 rd get key1"; return; }
  [ -z "$_RD" ] && { _rd_init || return 1; }
  eval "$(_rd_cli "$@")"
}

# 进入交互式 redis-cli shell
rds() {
  [[ "$1" == "-h" ]] && { echo "rds  # 进入交互式 redis-cli shell"; return; }
  [ -z "$_RD" ] && { _rd_init || return 1; }
  if [ -n "$RD_POD" ]; then
    # Pod ns 优先用 RD_POD_NS（支持 secret/pod 分属不同 ns），回退到 RD_NS
    local pod_ns="${RD_POD_NS:-${RD_NS:-tce}}" container="${RD_CONTAINER:-redis}"
    local cmd="redis-cli -h $_RD_H -p $_RD_P${_RD_AUTH:+ -a $_RD_AUTH}"
    kubectl exec -it "$RD_POD" -n "$pod_ns" -c "$container" -- bash -c "$cmd"
  else
    redis-cli -h "$_RD_H" -p "$_RD_P" ${_RD_AUTH:+-a "$_RD_AUTH"}
  fi
}

# 查集群/实例信息
rdi() {
  [[ "$1" == "-h" ]] && { echo "rdi [-s|-r|-c|-m]  # info全量/-s服务器/-r复制/-c客户端/-m内存"; return; }
  [ -z "$_RD" ] && { _rd_init || return 1; }
  case "$1" in
    -s) rd INFO server ;;
    -r) rd INFO replication ;;
    -c) rd INFO clients ;;
    -m) rd INFO memory ;;
    *)  rd INFO all ;;
  esac
}

# 扫描 key（替代 KEYS，不阻塞）
rdk() {
  [[ "$1" == "-h" ]] && { echo "rdk [pattern=*] [count=100]  # SCAN 扫描 key，不阻塞"; return; }
  [ -z "$_RD" ] && { _rd_init || return 1; }
  local pattern="${1:-*}" count="${2:-100}"
  rd SCAN 0 MATCH "$pattern" COUNT "$count"
}

# 查 key 详情：类型、TTL、值
rdg() {
  [[ "$1" == "-h" ]] && { echo "rdg <key>  # 查 key 类型/TTL/值"; return; }
  [ -z "$_RD" ] && { _rd_init || return 1; }
  local key="$1"
  [ -z "$key" ] && { echo "rdg <key>" >&2; return 1; }
  local type ttl
  type=$(rd TYPE "$key" 2>/dev/null | tr -d '\r')
  ttl=$(rd TTL "$key" 2>/dev/null | tr -d '\r')
  echo "key=$key  type=$type  ttl=${ttl}s" >&2
  case "$type" in
    string) rd GET "$key" ;;
    list)   rd LRANGE "$key" 0 9 ;;
    hash)   rd HGETALL "$key" ;;
    set)    rd SMEMBERS "$key" ;;
    zset)   rd ZRANGE "$key" 0 9 WITHSCORES ;;
    *)      echo "unknown type: $type" >&2 ;;
  esac
}

# 查大 key（内存占用统计，前 N 个最大 key）
rdm() {
  [[ "$1" == "-h" ]] && { echo "rdm [pattern=*] [N=20]  # 找大 key（按内存占用排序）"; return; }
  [ -z "$_RD" ] && { _rd_init || return 1; }
  local pattern="${1:-*}" n="${2:-20}"
  rd --bigkeys -i 0.01 2>&1 | grep -E "biggest|Biggest|sampled" | head -"$n"
}

# 切换 secret（方便同会话操作多个实例）
# 支持 secret 与 pod 分属不同 namespace 的场景（如 secret 在 tce，pod 在 sso）
# 用法: rdx <secret> [secret-ns] [pod] [container] [pod-ns]
rdx() {
  if [[ "$1" == "-h" || -z "$1" ]]; then
    echo "rdx <secret> [secret-ns] [pod] [container] [pod-ns]  # 切换 Redis secret/pod"
    echo "  secret-ns: secret 所在 namespace（默认 tce）"
    echo "  pod-ns:    pod 所在 namespace（默认同 secret-ns；secret/pod 不同 ns 时单独指定）"
    echo "  示例（secret 在 tce，pod 在 sso）:"
    echo "    rdx redis-apm tce redis-tce-redis-apm-ss-0 redis sso"
    return
  fi
  RD_SECRET="$1"
  [ -n "$2" ] && RD_NS="$2"
  [ -n "$3" ] && RD_POD="$3"
  [ -n "$4" ] && RD_CONTAINER="$4"
  [ -n "$5" ] && RD_POD_NS="$5"
  unset _RD _RD_H _RD_P _RD_U _RD_W _RD_AUTH
  local pod_ns_info=""
  [ -n "$RD_POD_NS" ] && pod_ns_info=" pod-ns=$RD_POD_NS"
  echo "已切换: secret=$RD_SECRET secret-ns=${RD_NS:-tce} pod=${RD_POD:-(直连)}${pod_ns_info} container=${RD_CONTAINER:-redis}" >&2
}
