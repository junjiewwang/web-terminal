#!/usr/bin/env bash
# K8s 镜像版本排查 Snippet — 精简版，粘贴到终端即可用
# 详细帮助: https://git.woa.com/junjiewwang/troubleshoot-snippets/blob/master/k8s/README.md

ki() {
  [[ "$1" == "-h" ]] && { echo "ki [-p] [namespace] [name-filter]"; return; }
  local pod_mode=false
  [[ "$1" == "-p" ]] && pod_mode=true && shift
  local ns="$1" filter="$2" ns_flag=""
  [ -n "$ns" ] && ns_flag="-n $ns" || ns_flag="--all-namespaces"
  if $pod_mode; then
    _ki_pod "$ns_flag" "$filter"
  else
    _ki_owner "$ns_flag" "$filter"
  fi
}

_ki_pod() {
  local ns_flag="$1" filter="$2" output
  output=$(kubectl get pods $ns_flag -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{range .spec.containers[*]}{.name}{"\t"}{.image}{"\n"}{end}{end}' 2>/dev/null)
  [ -z "$output" ] && { echo "No pods found" >&2; return 1; }
  ( echo -e "NAMESPACE\tPOD\tCONTAINER\tIMAGE"
    [ -n "$filter" ] && echo "$output" | grep -i "$filter" || echo "$output"
  ) | column -t -s $'\t'
}

_ki_owner() {
  local ns_flag="$1" filter="$2" output=""
  local deps sts ds
  deps=$(kubectl get deploy $ns_flag -o jsonpath='{range .items[*]}{.metadata.namespace}{"\tdeploy\t"}{.metadata.name}{"\t"}{range .spec.template.spec.containers[*]}{.name}{"\t"}{.image}{"\n"}{end}{end}' 2>/dev/null)
  [ -n "$deps" ] && output="$deps"
  sts=$(kubectl get sts $ns_flag -o jsonpath='{range .items[*]}{.metadata.namespace}{"\tsts\t"}{.metadata.name}{"\t"}{range .spec.template.spec.containers[*]}{.name}{"\t"}{.image}{"\n"}{end}{end}' 2>/dev/null)
  [ -n "$sts" ] && output="${output:+${output}\n}$sts"
  ds=$(kubectl get ds $ns_flag -o jsonpath='{range .items[*]}{.metadata.namespace}{"\tds\t"}{.metadata.name}{"\t"}{range .spec.template.spec.containers[*]}{.name}{"\t"}{.image}{"\n"}{end}{end}' 2>/dev/null)
  [ -n "$ds" ] && output="${output:+${output}\n}$ds"
  [ -z "$output" ] && { echo "No deployments/statefulsets/daemonsets found" >&2; return 1; }
  ( echo -e "NAMESPACE\tTYPE\tOWNER\tCONTAINER\tIMAGE"
    [ -n "$filter" ] && echo -e "$output" | grep -i "$filter" || echo -e "$output"
  ) | column -t -s $'\t'
}

kic() {
  [[ "$1" == "-h" ]] && { echo "kic <ns1> <ns2> [name-filter]"; return; }
  local ns1="$1" ns2="$2" filter="$3"
  [ -z "$ns2" ] && { echo "kic <ns1> <ns2> [name-filter]" >&2; return 1; }
  _kic_get() {
    kubectl get deploy -n "$1" -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .spec.template.spec.containers[*]}{.name}{"="}{.image}{" "}{end}{"\n"}{end}' 2>/dev/null
  }
  local tmp1 tmp2
  tmp1=$(mktemp) && tmp2=$(mktemp)
  trap "rm -f $tmp1 $tmp2" RETURN
  _kic_get "$ns1" | sort > "$tmp1"
  _kic_get "$ns2" | sort > "$tmp2"
  if [ -n "$filter" ]; then
    local f1 f2; f1=$(mktemp) && f2=$(mktemp)
    trap "rm -f $tmp1 $tmp2 $f1 $f2" RETURN
    grep -i "$filter" "$tmp1" > "$f1"; grep -i "$filter" "$tmp2" > "$f2"
    tmp1="$f1"; tmp2="$f2"
  fi
  echo -e "DEPLOYMENT\t${ns1}\t${ns2}\tSTATUS"
  echo -e "----------\t$(echo $ns1 | sed 's/./-/g')\t$(echo $ns2 | sed 's/./-/g')\t------"
  ( awk '{print $1}' "$tmp1" "$tmp2" | sort -u | while read -r name; do
      img1=$(grep "^${name}" "$tmp1" 2>/dev/null | cut -f2-)
      img2=$(grep "^${name}" "$tmp2" 2>/dev/null | cut -f2-)
      local status="OK"
      [ -z "$img1" ] && img1="(not found)" && status="← DIFF"
      [ -z "$img2" ] && img2="(not found)" && status="← DIFF"
      [ "$img1" != "$img2" ] && status="← DIFF"
      echo -e "${name}\t${img1}\t${img2}\t${status}"
    done
  ) | column -t -s $'\t'
}
