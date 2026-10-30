#!/usr/bin/env bash
# ldo-filter 一键安装为 macOS launchd 常驻服务
# 用法：./install.sh          安装并启动
#       ./install.sh status   查看状态
#       ./install.sh stop     停止并卸载
#       ./install.sh logs     跟踪日志
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.ldo.filter"
PLIST_SRC="$DIR/launchd/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3)"

action="${1:-install}"

case "$action" in
  install)
    echo "==> 预检"
    cd "$DIR"
    "$PY" -m ldo doctor || {
      echo
      echo "自检未通过，先修 ❌ 的项再装。" >&2
      exit 1
    }

    echo
    echo "==> 生成 plist（路径已按当前目录注入）"
    mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"
    sed -e "s|/opt/homebrew/bin/python3|$PY|g" \
        -e "s|/Users/liszt/Documents/deepseek-harness/default-workspace/ldo-filter|$DIR|g" \
        "$PLIST_SRC" > "$PLIST_DST"
    echo "    $PLIST_DST"

    echo "==> 卸载旧实例（如有）"
    launchctl unload -w "$PLIST_DST" 2>/dev/null || true

    echo "==> 加载"
    launchctl load -w "$PLIST_DST"

    sleep 3
    echo
    echo "==> 状态"
    launchctl list | grep "$LABEL" || echo "  未在列表中出现，看日志："
    echo
    echo "已启动。日志："
    echo "  tail -f $DIR/logs/run.log"
    echo "  tail -f $DIR/logs/run.err.log"
    echo
    echo "首次运行会拉最新一页并推送。要看积压的高分内容："
    echo "  cd $DIR && $PY -m ldo show -n 30"
    ;;

  status)
    launchctl list | grep "$LABEL" || echo "未安装/未运行"
    echo "--- 最近日志 ---"
    tail -n 20 "$DIR/logs/run.log" 2>/dev/null || echo "(暂无日志)"
    ;;

  stop)
    launchctl unload -w "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "已停止并移除 $PLIST_DST"
    ;;

  logs)
    tail -f "$DIR/logs/run.log" "$DIR/logs/run.err.log"
    ;;

  *)
    echo "用法：$0 [install|status|stop|logs]" >&2
    exit 1
    ;;
esac
