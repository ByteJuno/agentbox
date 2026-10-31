#!/bin/bash
# 一次性：等 hub 空闲后把 ~/ai-work/agent-hub 换成指向 fork 的 hub/ 目录（2026-10-31）。失败自动回滚。
set -u
LOG=/home/ubuntu/ai-work/agentbox/hub/ops/logs/deploy-switch.log
OLD=/home/ubuntu/ai-work/agent-hub
BAK=/home/ubuntu/ai-work/agent-hub.old-20261031
NEW=/home/ubuntu/ai-work/agentbox/hub
TOK=$(cat /home/ubuntu/.agent-hub/token)
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
notify() { /usr/bin/python3 -c "import sys; sys.path.insert(0,'/opt/homelab-report'); import ops_notify; ops_notify.send_private(sys.argv[1])" "$1" >/dev/null 2>&1 || true; }
busy() {
  curl -s -m 10 -H "Authorization: Bearer $TOK" http://127.0.0.1:8765/hub/sessions | /usr/bin/python3 -c "
import json,sys
d=json.load(sys.stdin)
sys.exit(0 if any(s['status'] in ('running','waiting') or s.get('queue') for s in d['sessions']) else 1)" 
}
log "waiting for idle"
idle=0
for i in $(seq 1 2880); do   # 最多等 24 小时
  if busy; then idle=0; else idle=$((idle+1)); fi
  [ $idle -ge 3 ] && break
  sleep 30
done
[ $idle -ge 3 ] || { log "never idle, giving up"; exit 1; }
[ -L "$OLD" ] && { log "already switched"; exit 0; }
log "idle; switching"
systemctl --user stop agent-hub
mv "$OLD" "$BAK" && ln -s "$NEW" "$OLD"
systemctl --user start agent-hub
ok=0
for i in $(seq 1 30); do
  sleep 2
  if curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/api/ping | grep -q 200 && \
     curl -s -m 20 -H "Authorization: Bearer $TOK" http://127.0.0.1:8765/api/me | grep -q '"'; then ok=1; break; fi
done
if [ $ok = 1 ]; then
  log "switched OK: $(readlink "$OLD") $(git -C /home/ubuntu/ai-work/agentbox rev-parse --short HEAD)"
  notify "agent-hub 已切到 fork（ByteJuno/agentbox hub 分支 $(git -C /home/ubuntu/ai-work/agentbox rev-parse --short HEAD)），网页刷新即可。旧目录在 $BAK"
else
  log "new hub failed, rolling back"
  systemctl --user stop agent-hub
  rm -f "$OLD" && mv "$BAK" "$OLD"
  systemctl --user start agent-hub
  log "rolled back"
  notify "agent-hub 切换到 fork 失败，已回滚到旧版本。日志：$LOG"
fi
