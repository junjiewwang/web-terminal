#!/usr/bin/env bash
# MySQL 排查 Snippet — 精简版，粘贴到终端即可用
# 详细帮助: https://git.woa.com/junjiewwang/troubleshoot-snippets/blob/master/mysql/README.md

# 从 k8s secret 初始化连接信息并缓存
_my_init() {
  local ns="${MY_NS:-tce}" secret="${MY_SECRET}"
  [ -z "$secret" ] && { echo "MY_SECRET 未设置，请 export MY_SECRET=<secret-name>" >&2; return 1; }
  local get="kubectl get secret -n $ns $secret -o jsonpath"
  _MY_H=$($get='{.data.host}' | base64 -d)
  _MY_P=$($get='{.data.port}' | base64 -d)
  _MY_U=$($get='{.data.user}' | base64 -d)
  _MY_W=$($get='{.data.pass}' | base64 -d)
  _MY=1 && echo "MySQL: $_MY_U@$_MY_H:$_MY_P" >&2
}

# 基础连接：进入交互式 mysql shell
my() {
  [[ "$1" == "-h" ]] && { echo "my [db]  # 连接 MySQL shell (可选指定数据库)"; return; }
  [ -z "$_MY" ] && { _my_init || return 1; }
  local db="$1"
  [ -n "$db" ] && mysql -h"$_MY_H" -P"$_MY_P" -u"$_MY_U" -p"$_MY_W" "$db" \
                || mysql -h"$_MY_H" -P"$_MY_P" -u"$_MY_U" -p"$_MY_W"
}

# 执行单条 SQL，结果表格对齐输出
myq() {
  [[ "$1" == "-h" ]] && { echo "myq <sql> [db]  # 执行 SQL 并格式化输出"; return; }
  [ -z "$_MY" ] && { _my_init || return 1; }
  local sql="$1" db="$2"
  [ -z "$sql" ] && { echo "myq <sql> [db]" >&2; return 1; }
  local args=(-h"$_MY_H" -P"$_MY_P" -u"$_MY_U" -p"$_MY_W" -e "$sql")
  [ -n "$db" ] && args+=("$db")
  mysql "${args[@]}" 2>/dev/null | column -t -s $'\t'
}

# 列出所有数据库或指定库的表
myl() {
  [[ "$1" == "-h" ]] && { echo "myl [db]  # 列出所有库，或指定库的所有表"; return; }
  [ -z "$_MY" ] && { _my_init || return 1; }
  local db="$1"
  [ -n "$db" ] && myq "SHOW TABLES;" "$db" || myq "SHOW DATABASES;"
}

# 查进程列表 / 慢查询 / 锁等待
myps() {
  [[ "$1" == "-h" ]] && { echo "myps [-l|-w|-k <id>]  # 进程列表/-l慢查询/-w锁等待/-k结束进程"; return; }
  [ -z "$_MY" ] && { _my_init || return 1; }
  case "$1" in
    -l) myq "SELECT id,user,host,db,time,state,LEFT(info,80) info FROM information_schema.processlist WHERE time>5 ORDER BY time DESC;" ;;
    -w) myq "SELECT r.trx_id waiting_trx, r.trx_mysql_thread_id waiting_thread, r.trx_query waiting_query,
              b.trx_id blocking_trx, b.trx_mysql_thread_id blocking_thread, b.trx_query blocking_query
              FROM information_schema.innodb_lock_waits w
              JOIN information_schema.innodb_trx b ON b.trx_id=w.blocking_trx_id
              JOIN information_schema.innodb_trx r ON r.trx_id=w.requesting_trx_id;" ;;
    -k) [ -z "$2" ] && { echo "myps -k <thread_id>" >&2; return 1; }
        myq "KILL $2;" ;;
    *)  myq "SELECT id,user,host,db,command,time,state,LEFT(info,80) info FROM information_schema.processlist ORDER BY time DESC;" ;;
  esac
}

# 查表状态：大小、行数、引擎
myt() {
  [[ "$1" == "-h" ]] && { echo "myt <db> [table-filter]  # 查表大小/行数/引擎"; return; }
  [ -z "$_MY" ] && { _my_init || return 1; }
  local db="$1" filter="${2:+AND table_name LIKE '%$2%'}"
  [ -z "$db" ] && { echo "myt <db> [table-filter]" >&2; return 1; }
  myq "SELECT table_name,engine,table_rows,
         ROUND((data_length+index_length)/1024/1024,2) AS size_mb,
         ROUND(data_length/1024/1024,2) AS data_mb,
         ROUND(index_length/1024/1024,2) AS idx_mb
         FROM information_schema.tables
         WHERE table_schema='$db' $filter
         ORDER BY (data_length+index_length) DESC;"
}

# 切换 secret（方便同会话操作多个实例）
mys() {
  [[ "$1" == "-h" || -z "$1" ]] && { echo "mys <secret> [ns]  # 切换 MySQL secret"; return; }
  MY_SECRET="$1"
  [ -n "$2" ] && MY_NS="$2"
  unset _MY _MY_H _MY_P _MY_U _MY_W
  echo "已切换到 secret=$MY_SECRET ns=${MY_NS:-tce}，下次调用时重新初始化" >&2
}
