#!/usr/bin/env bash
# File Transfer Snippet — PTY 通道文件传输（精简版）
# 兼容 bash / zsh
__FT_SNIPPET_VERSION__="2026.05.14.5"

# ── ft_recv [--compressed] <path> ─────────────
ft_recv() {
  local _compressed=0 target=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --compressed) _compressed=1; shift ;;
      *) target="$1"; shift ;;
    esac
  done

  [ -z "$target" ] && { echo "__FT_RECV_ERR__:missing target path"; return 1; }

  if [ "$_compressed" -eq 1 ]; then
    command -v gunzip >/dev/null 2>&1 || command -v gzip >/dev/null 2>&1 || {
      echo "__FT_RECV_ERR__:gunzip not available"; return 1; }
  fi

  local dir; dir=$(dirname "$target")
  mkdir -p "$dir" 2>/dev/null || { echo "__FT_RECV_ERR__:cannot create dir $dir"; return 1; }

  local tmp; tmp=$(mktemp "${target}.b64.XXXXXX") || {
    echo "__FT_RECV_ERR__:cannot create temp file"; return 1; }

  # base64 解码辅助（兼容 Linux/macOS）
  _b64dec() { base64 -d 2>/dev/null || base64 --decode 2>/dev/null; }

  local _old_stty; _old_stty=$(stty -g 2>/dev/null)
  stty -echo -icanon 2>/dev/null

  _ft_cleanup() {
    stty "$_old_stty" 2>/dev/null
    [ -s "$target" ] && rm -f "$tmp" "${target}.recv.gz"
    trap - INT TERM HUP
  }
  trap '_ft_cleanup; echo ""; echo "__FT_RECV_ERR__:interrupted"; return 130' INT TERM HUP

  echo "__FT_RECV_READY__"

  local _timeout=300 _got_eof=0 _count=0 _exp=0
  while IFS= read -r -t "$_timeout" line; do
    case "$line" in
      __FT_EOF__*) _got_eof=1; break ;;
      __FT_CHUNK__:*)
        local _p="${line#__FT_CHUNK__:}" _s="${line#__FT_CHUNK__:}"
        _s="${_s%%:*}"; _p="${_p#*:}"
        if [ "$_s" != "$_exp" ]; then
          echo "__FT_ACK__:${_s}:SEQ_ERR:${_exp}"; continue; fi
        if ! printf '%s' "$_p" | _b64dec >/dev/null 2>&1; then
          echo "__FT_ACK__:${_s}:CORRUPT"; continue; fi
        printf '%s\n' "$_p" >> "$tmp"
        _count=$((_count + 1)); _exp=$((_exp + 1))
        echo "__FT_ACK__:${_s}:OK" ;;
    esac
  done

  if [ "$_got_eof" -eq 0 ]; then
    _ft_cleanup; echo "__FT_RECV_ERR__:read timeout (${_timeout}s)"; return 1; fi

  # 解码写入（压缩模式先 gunzip）
  if [ "$_compressed" -eq 1 ]; then
    local gz="${target}.recv.gz"
    if _b64dec < "$tmp" > "$gz" && { gunzip -c "$gz" > "$target" 2>/dev/null || gzip -d -c "$gz" > "$target" 2>/dev/null; }; then
      rm -f "$gz"
    else
      rm -f "$gz" "$target"; _ft_cleanup; echo "__FT_RECV_ERR__:decompress failed"; return 1
    fi
  else
    if ! _b64dec < "$tmp" > "$target"; then
      rm -f "$target"; _ft_cleanup; echo "__FT_RECV_ERR__:base64 decode failed"; return 1
    fi
  fi

  local sz; sz=$(wc -c < "$target" | tr -d ' ')
  _ft_cleanup; echo "__FT_RECV_OK__:${sz}"
}

# ── ft_send [--compressed] <path> [chunk_kb] ──
ft_send() {
  local _compressed=0 src="" ckb=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --compressed) _compressed=1; shift ;;
      *) if [ -z "$src" ]; then src="$1"; else ckb="$1"; fi; shift ;;
    esac
  done
  ckb="${ckb:-36}"

  [ -z "$src" ] && { echo "__FT_SEND_ERR__:missing source path"; return 1; }
  [ ! -f "$src" ] && { echo "__FT_SEND_ERR__:file not found: $src"; return 1; }
  [ ! -r "$src" ] && { echo "__FT_SEND_ERR__:permission denied: $src"; return 1; }

  local orig_sz; orig_sz=$(wc -c < "$src" | tr -d ' ')

  # 压缩模式：gzip 到临时文件
  local actual_src="$src" actual_sz="$orig_sz" gz_tmp=""
  if [ "$_compressed" -eq 1 ]; then
    if command -v gzip >/dev/null 2>&1; then
      gz_tmp=$(mktemp "${src}.send.gz.XXXXXX") || {
        echo "__FT_SEND_ERR__:cannot create temp file"; return 1; }
      if gzip -c "$src" > "$gz_tmp" 2>/dev/null; then
        actual_src="$gz_tmp"
        actual_sz=$(wc -c < "$gz_tmp" | tr -d ' ')
      else
        rm -f "$gz_tmp"; gz_tmp=""
        _compressed=0  # gzip 失败，回退为无压缩
      fi
    else
      _compressed=0  # gzip 不可用，回退
    fi
  fi

  # 抑制 PTY 回显（避免 base64 数据刷屏终端）
  local _old_stty; _old_stty=$(stty -g 2>/dev/null)
  stty -echo 2>/dev/null

  _ft_send_cleanup() {
    stty "$_old_stty" 2>/dev/null
    [ -n "$gz_tmp" ] && rm -f "$gz_tmp"
    trap - INT TERM HUP
  }
  trap '_ft_send_cleanup; echo "__FT_SEND_ERR__:interrupted"; return 130' INT TERM HUP

  # BEGIN 标记：compressed 时格式为 <compressed_sz>:<orig_sz>:C
  if [ "$_compressed" -eq 1 ]; then
    echo "__FT_SEND_BEGIN__:${actual_sz}:${orig_sz}:C"
  else
    echo "__FT_SEND_BEGIN__:${actual_sz}"
  fi

  # MD5 校验和（对原始文件计算）
  local md5
  if command -v md5sum >/dev/null 2>&1; then md5=$(md5sum "$src" | awk '{print $1}')
  elif command -v md5 >/dev/null 2>&1; then md5=$(md5 -q "$src")
  else md5="unavailable"; fi

  # 分块发送：dd bs=cbs skip=blk count=1（高效块读取，替代 bs=1 逐字节）
  # skip 以 bs 为单位跳过块，所有平台兼容；最后不完整块 dd 自动返回实际字节数
  local cbs=$(( ckb * 1024 / 3 * 3 )) blk=0 rem="$actual_sz"
  while [ "$rem" -gt 0 ]; do
    local rs="$cbs"; [ "$rem" -lt "$cbs" ] && rs="$rem"
    echo "__FT_CHUNK__:$(dd if="$actual_src" bs="$cbs" skip="$blk" count=1 2>/dev/null | base64 | tr -d '\n')"
    blk=$((blk + 1)); rem=$((rem - rs)); sleep 0.005
  done
  echo "__FT_SEND_END__:${md5}"
  _ft_send_cleanup
}
