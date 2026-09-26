#!/usr/bin/env bash
set -euo pipefail
RELEASE=${1:?release directory required}
BASE=/home/ubuntu/strategy-control
LEGACY=/home/ubuntu/flattrade_tb/flattrade_tb
mkdir -p "$BASE"
chmod 700 "$BASE"
# Capture service state for rollback; legacy source and account data are untouched.
systemctl is-enabled trading-scheduler.service > "$BASE/previous-scheduler-enabled.txt" 2>/dev/null || true
systemctl is-active trading-scheduler.service > "$BASE/previous-scheduler-active.txt" 2>/dev/null || true
if [ -L "$BASE/current" ]; then readlink "$BASE/current" > "$BASE/previous-release.txt"; fi
python3 - "$BASE" "$LEGACY" <<'PY'
import ast,os,sys
from pathlib import Path
base,legacy=map(Path,sys.argv[1:])
user=''
p=legacy/'creds.py'
if p.exists():
 for node in ast.walk(ast.parse(p.read_text())):
  if isinstance(node,ast.Assign) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):
   if any(isinstance(t,ast.Name) and t.id.upper() in ('USER_ID','USERID','UID') for t in node.targets):user=node.value.value
lines=['FLATTRADE_TOKEN_FILE='+str(legacy/'token.txt')]
if user.isalnum():lines.append('FLATTRADE_USER_ID='+user)
f=base/'runtime.env';fd=os.open(f,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as h:h.write('\n'.join(lines)+'\n')
PY
ln -sfn "$RELEASE" "$BASE/current"
cat > "$BASE/strategy-control.service" <<EOF
[Unit]
Description=TradeDesk authenticated paper strategy controls
After=network-online.target
Wants=network-online.target
[Service]
User=ubuntu
WorkingDirectory=$BASE/current
EnvironmentFile=$BASE/runtime.env
ExecStart=/usr/bin/python3 -u $BASE/current/control_dashboard.py --host 0.0.0.0 --allowed-host 54.162.151.193 --port 8080 --root $LEGACY --token-file $BASE/control.token
Restart=on-failure
RestartSec=5
TimeoutStopSec=50
UMask=0077
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
EOF
cat > "$BASE/strategy-control-tunnel.service" <<EOF
[Unit]
Description=TradeDesk HTTPS tunnel
After=network-online.target strategy-control.service
Requires=strategy-control.service
[Service]
User=ubuntu
WorkingDirectory=$BASE/current
ExecStart=/usr/bin/python3 -u $BASE/current/deploy/tunnel.py --origin-file $BASE/public-origin.txt --port 8080
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
EOF
sudo install -m 644 "$BASE/strategy-control.service" /etc/systemd/system/strategy-control.service
sudo install -m 644 "$BASE/strategy-control-tunnel.service" /etc/systemd/system/strategy-control-tunnel.service
sudo systemctl daemon-reload
sudo systemctl enable strategy-control.service
sudo systemctl disable --now strategy-control-tunnel.service || true
sudo systemctl restart strategy-control.service
# Old unattended scheduler is retired; its dashboard/history remains available.
sudo systemctl disable --now trading-scheduler.service
systemctl is-active strategy-control.service
