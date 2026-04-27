#!/usr/bin/env bash
# ES 排查 Snippet — 精简版，粘贴到终端即可用
# 详细帮助: https://git.woa.com/junjiewwang/troubleshoot-snippets/blob/master/es/README.md

es() {
  [[ "$1" == "-h" ]] && { echo "es <path> [curl参数...]"; return; }
  if [ -z "$_ES" ]; then
    local ns="${ES_NS:-tce}" secret="${ES_SECRET:-ark-monitor-es}"
    local get="kubectl get secret -n $ns $secret -o jsonpath"
    _ES_U=$($get='{.data.user}' | base64 -d)
    _ES_P=$($get='{.data.password}' | base64 -d)
    _ES_H=$($get='{.data.host}' | base64 -d):$($get='{.data.port}' | base64 -d)
    _ES=1 && echo "ES: $_ES_U@$_ES_H" >&2
  fi
  local path="$1"; shift
  [ -z "$path" ] && { echo "es <path> [curl参数...]" >&2; return 1; }
  local args=(-s -u "$_ES_U:$_ES_P")
  [[ "$*" == *"-d "* ]] && args+=(-H "Content-Type: application/json")
  curl "${args[@]}" "$_ES_H$path" "$@" 2>/dev/null | python3 -m json.tool 2>/dev/null || curl "${args[@]}" "$_ES_H$path" "$@"
}

esl() {
  [[ "$1" == "-h" ]] && { echo "esl <index> [N=10] [field...]"; return; }
  local idx="$1" n="${2:-10}"; [ -z "$idx" ] && { echo "esl <index> [N=10] [field...]" >&2; return 1; }
  shift; shift 2>/dev/null
  local src="true" ts="${ES_TS:-timestamp}"
  [ $# -gt 0 ] && src=$(printf '"%s",' "$@" | sed 's/,$//' | sed 's/^/[/;s/$/]/')
  es "/$idx/_search" -d "{\"size\":$n,\"sort\":[{\"$ts\":{\"order\":\"desc\"}}],\"_source\":$src}"
}

esr() {
  [[ "$1" == "-h" ]] && { echo "esr <index> <time> [N=10] [-f 'filter'] [field...]"; return; }
  local idx="$1" range="$2" n="${3:-10}"
  [ -z "$range" ] && { echo "esr <index> <5m|1h|2d|from,to> [N=10] [-f 'filter'] [field...]" >&2; return 1; }
  shift 2; shift 2>/dev/null
  local extra=""
  [ "$1" = "-f" ] && { extra=",$2"; shift 2; }
  local src="true" ts="${ES_TS:-timestamp}" from to
  if [[ "$range" == *","* ]]; then
    from=${range%,*}; to=${range#*,}
    [ ${#from} -le 10 ] && from=$((from * 1000))
    [ ${#to} -le 10 ] && to=$((to * 1000))
  else
    local num=${range%[smhd]} unit=${range##*[0-9]} ms=0
    case "$unit" in
      s) ms=$((num * 1000)) ;; m) ms=$((num * 60000)) ;;
      h) ms=$((num * 3600000)) ;; d) ms=$((num * 86400000)) ;;
      *) echo "格式无效" >&2; return 1 ;;
    esac
    to=$(python3 -c "import time;print(int(time.time()*1000))")
    from=$((to - ms))
  fi
  [ $# -gt 0 ] && src=$(printf '"%s",' "$@" | sed 's/,$//' | sed 's/^/[/;s/$/]/')
  es "/$idx/_search" -d "{\"size\":$n,\"sort\":[{\"$ts\":{\"order\":\"desc\"}}],\"_source\":$src,\"query\":{\"bool\":{\"filter\":[{\"range\":{\"$ts\":{\"gte\":$from,\"lte\":$to}}}$extra]}}}"
}

esq() {
  [[ "$1" == "-h" ]] && { echo "esq <index> <field> <value> [N=10]"; return; }
  local idx="$1" field="$2" val="$3" n="${4:-10}" ts="${ES_TS:-timestamp}"
  [ -z "$val" ] && { echo "esq <index> <field> <value> [N=10]" >&2; return 1; }
  es "/$idx/_search" -d "{\"size\":$n,\"sort\":[{\"$ts\":{\"order\":\"desc\"}}],\"query\":{\"term\":{\"$field\":\"$val\"}}}"
}

esm() {
  [[ "$1" == "-h" ]] && { echo "esm <index> [field...]"; return; }
  local idx="$1"; shift
  [ -z "$idx" ] && { echo "esm <index> [field...]" >&2; return 1; }
  [ $# -eq 0 ] && { es "/$idx/_mapping"; return; }
  es "/$idx/_mapping/field/$(IFS=,; echo "$*")"
}

esn() {
  [[ "$1" == "-h" ]] && { echo "esn [-r|-s|-t] [node]  # 节点概览/-r角色/-s资源/-t线程池/node详情"; return; }
  case "$1" in
    -r) es "/_cat/nodes?v&h=name,ip,role&s=role" ;;
    -s) es "/_cat/nodes?v&h=name,ip,heap.percent,heap.max,ram.percent,disk.used_percent,disk.avail&s=disk.used_percent:desc" ;;
    -t) es "/_cat/thread_pool?v&h=node_name,name,active,queue,rejected&s=rejected:desc,queue:desc" ;;
    "") es "/_cat/nodes?v&h=name,ip,role,heap.percent,disk.used_percent,load_1m&s=name" ;;
    *)  es "/_nodes/$1/stats" ;;
  esac
}

ess() {
  [[ "$1" == "-h" ]] && { echo "ess [-u|-e|-r] [index]  # 分片总览/-u未分配/-e分配失败原因/-r分布/index指定索引"; return; }
  case "$1" in
    -u) es "/_cat/shards?v&h=index,shard,prirep,state,unassigned.reason,node&s=state" | grep -E "^index|UNASSIGNED" ;;
    -e) [ -n "$2" ] && es "/_cluster/allocation/explain" -d "{\"index\":\"$2\",\"shard\":0,\"primary\":false}" || es "/_cluster/allocation/explain" ;;
    -r) es "/_cat/shards/${2:-*}?v&h=index,shard,prirep,state,docs,store,node&s=index,shard,prirep" ;;
    "") es "/_cat/shards?v&s=index,shard,prirep" ;;
    *)  es "/_cat/shards/$1?v&s=shard,prirep" ;;
  esac
}

esa() {
  [[ "$1" == "-h" || -z "$1" ]] && { echo "esa replica <idx> <n> | shard <idx> | route <enable|disable> [idx] | rebalance <enable|disable> | retry"; return; }
  case "$1" in
    replica)   es "/$2/_settings" -X PUT -d "{\"index\":{\"number_of_replicas\":$3}}" ;;
    shard)     echo "⚠️ 主分片数不可热改，需 reindex" >&2; es "/$2/_settings?filter_path=*.settings.index.number_of_shards,*.settings.index.number_of_replicas" ;;
    route)
      local val; [[ "$2" == "enable" ]] && val="all" || val="none"
      [ -n "$3" ] && es "/$3/_settings" -X PUT -d "{\"index.routing.allocation.enable\":\"$val\"}" \
                  || es "/_cluster/settings" -X PUT -d "{\"transient\":{\"cluster.routing.allocation.enable\":\"$val\"}}" ;;
    rebalance)
      local val; [[ "$2" == "enable" ]] && val="all" || val="none"
      es "/_cluster/settings" -X PUT -d "{\"transient\":{\"cluster.routing.rebalance.enable\":\"$val\"}}" ;;
    retry)     es "/_cluster/reroute?retry_failed" -X POST ;;
    *)         echo "未知操作，运行 esa -h" >&2 ;;
  esac
}
