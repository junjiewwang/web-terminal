#!/usr/bin/env bash
# File Transfer Snippet — PTY 通道文件传输（精简版）
# 兼容 bash / zsh
__FT_SNIPPET_VERSION__="2026.05.14.3"

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

# ── ft_send <path> [chunk_kb] ─────────────────
ft_send() {
  local src="$1" ckb="${2:-2}"
  [ -z "$src" ] && { echo "__FT_SEND_ERR__:missing source path"; return 1; }
  [ ! -f "$src" ] && { echo "__FT_SEND_ERR__:file not found: $src"; return 1; }
  [ ! -r "$src" ] && { echo "__FT_SEND_ERR__:permission denied: $src"; return 1; }

  local sz; sz=$(wc -c < "$src" | tr -d ' ')
  echo "__FT_SEND_BEGIN__:${sz}"

  local md5
  if command -v md5sum >/dev/null 2>&1; then md5=$(md5sum "$src" | awk '{print $1}')
  elif command -v md5 >/dev/null 2>&1; then md5=$(md5 -q "$src")
  else md5="unavailable"; fi

  local cbs=$(( ckb * 1024 / 3 * 3 )) off=0 rem="$sz"
  while [ "$rem" -gt 0 ]; do
    local rs="$cbs"; [ "$rem" -lt "$cbs" ] && rs="$rem"
    echo "__FT_CHUNK__:$(dd if="$src" bs=1 skip="$off" count="$rs" 2>/dev/null | base64 | tr -d '\n')"
    off=$((off + rs)); rem=$((rem - rs)); sleep 0.05
  done
  echo "__FT_SEND_END__:${md5}"
}
