#!/bin/bash
# 等 hub 空闲后把线上 checkout（~/ai-work/agentbox，hub 分支）快进到指定提交并重启；失败自动回滚。
# 用法（必须脱离当前对话跑，hub 重启会结束对话里的进程）：
#   systemd-run --user --unit=agent-hub-update --collect hub/ops/deploy-update.sh <分支或提交>
# 前端静态文件由线上 checkout 直接提供，所以快进放在等到空闲之后，和重启挨在一起。
set -u
REPO=/home/ubuntu/ai-work/agentbox
LOG=$REPO/hub/ops/logs/deploy-update.log
TARGET=${1:?usage: deploy-update.sh <ref>}
TOK=$(cat /home/ubuntu/.agent-hub/token)
mkdir -p "$(dirname "$LOG")"
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
notify() { /usr/bin/python3 -c "import sys; sys.path.insert(0,'/opt/homelab-report'); import ops_notify; ops_notify.send_private(sys.argv[1])" "$1" >/dev/null 2>&1 || true; }
busy() {
  curl -s -m 10 -H "Authorization: Bearer $TOK" http://127.0.0.1:8765/hub/sessions | /usr/bin/python3 -c "
import json,sys
d=json.load(sys.stdin)
sys.exit(0 if any(s['status'] in ('running','waiting') or s.get('queue') for s in d['sessions']) else 1)"
}
healthy() {
  curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/api/ping | grep -q 200 && \
  curl -s -m 20 -H "Authorization: Bearer $TOK" http://127.0.0.1:8765/api/me | grep -q '"'
}
restart() {
  systemctl --user restart agent-hub
  for i in $(seq 1 30); do sleep 2; healthy && return 0; done
  return 1
}

NEW=$(git -C "$REPO" rev-parse --verify --quiet "$TARGET^{commit}") || { log "unknown ref $TARGET"; exit 1; }
log "waiting for idle to deploy $TARGET ($NEW)"
idle=0
for i in $(seq 1 2880); do   # 最多等 24 小时
  if busy; then idle=0; else idle=$((idle+1)); fi
  [ $idle -ge 3 ] && break
  sleep 30
done
[ $idle -ge 3 ] || { log "never idle, giving up"; exit 1; }

OLD=$(git -C "$REPO" rev-parse HEAD)
[ "$(git -C "$REPO" symbolic-ref --short HEAD)" = hub ] || { log "checkout not on hub branch, abort"; exit 1; }
# --ff-only 不会动无关的本地改动；有冲突的话直接失败，不重启
git -C "$REPO" merge -q --ff-only "$NEW" >> "$LOG" 2>&1 || { log "fast-forward failed, nothing changed"; notify "agent-hub 更新失败：无法快进到 ${NEW:0:7}，线上没动。日志：$LOG"; exit 1; }
log "idle; $OLD -> $NEW, restarting"
if restart; then
  log "updated OK"
  notify "agent-hub 已更新到 ${NEW:0:7}，网页刷新即可。"
else
  log "new hub unhealthy, rolling back to $OLD"
  git -C "$REPO" reset -q --keep "$OLD" >> "$LOG" 2>&1
  if restart; then log "rolled back"; else log "rollback also unhealthy"; fi
  notify "agent-hub 更新到 ${NEW:0:7} 后启动失败，已回滚到 ${OLD:0:7}。日志：$LOG"
fi
