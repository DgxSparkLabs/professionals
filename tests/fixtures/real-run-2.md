# Real run 2: a new professional answers its first practice task

Created by professional-smith (grow run), then asked by the user session with `pros ask systemd-hardening-auditor @practice.txt --wait 90`. The reply arrived via the auto-reply from the final message (run 1 showed the explicit reply command being denied).

```
[pro-msg id=m-4e64fda8c2 from=systemd-hardening-auditor to=user task=t-3c1a7e5f kind=reply reply-to=systemd-hardening-auditor chain=systemd-hardening-auditor]
Unit: python3 -m http.server 8080, User=root, no WorkingDirectory=, no sandbox.
Score: ESTIMATE ~9.6 UNSAFE (systemd-analyze not run: no shell access to it). Expect about 2 to 3 after the drop-in.
Gaps, heaviest first: root user; no NoNewPrivileges; full capability set; no filesystem protection; no kernel/process/syscall limits; all address families.
Service-specific: root with no WorkingDirectory= means http.server serves "/" (whole disk, incl. /etc/shadow, readable over HTTP). Set WorkingDirectory first; also bind 127.0.0.1 (--bind) or firewall it.
Drop-in /etc/systemd/system/<unit>.d/hardening.conf:
[Service]
# Group 1: identity/files (low risk)
DynamicUser=yes                    # risk: served files must be world-readable
WorkingDirectory=/srv/demo         # risk: dir must exist, else status=200/CHDIR
NoNewPrivileges=yes                # risk: none
PrivateTmp=yes                     # risk: no shared /tmp
ProtectSystem=strict               # risk: writes outside ReadWritePaths fail (server only reads)
ProtectHome=yes                    # risk: serving from /home breaks
PrivateDevices=yes                 # risk: none
UMask=0077                         # risk: none
# Group 2: kernel (very low risk)
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
LockPersonality=yes
ProtectProc=invisible
ProcSubset=pid                     # risk: reading /proc/sys fails; fine here
# Group 3: process/network (medium)
CapabilityBoundingSet=             # risk: port 8080 needs none; below 1024 add CAP_NET_BIND_SERVICE
RestrictAddressFamilies=AF_INET AF_INET6   # risk: add AF_UNIX if socket errors appear
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
RemoveIPC=yes
MemoryDenyWriteExecute=yes         # risk: breaks ctypes/JIT; plain http.server OK
# Group 4: syscalls (riskiest, last)
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources   # risk: SIGSYS, status=31/SYS; drop this line first if seen
Test: systemd-analyze verify <unit>; apply group 1, daemon-reload, restart, curl localhost:8080, journalctl -u <unit>; then groups 2, 3, 4 one at a time.
Rollback: rm the drop-in; systemctl daemon-reload; systemctl restart <unit>.
Not checked: real score on target systemd, runtime behaviour under the sandbox, the right directory for /srv/demo.

```
