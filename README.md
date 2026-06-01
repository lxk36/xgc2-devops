# XGC2 DevOps

```bash
curl -fsSL https://xgc2.apt.xiaokang.ink/xgc2-archive-keyring.gpg -o /tmp/xgc2-archive-keyring.gpg

gpg --show-keys --with-fingerprint --with-colons /tmp/xgc2-archive-keyring.gpg 2>&1 \
| grep -q '^fpr:\+2A8E11B36F56D307ADF626D85E5FDC30979EA43F:$' \
&& sudo install -d -m 0755 /etc/apt/keyrings \
&& cat /tmp/xgc2-archive-keyring.gpg \
| sudo tee /etc/apt/keyrings/xgc2-archive-keyring.gpg > /dev/null \
&& echo 'deb [signed-by=/etc/apt/keyrings/xgc2-archive-keyring.gpg] https://xgc2.apt.xiaokang.ink focal main' \
| sudo tee /etc/apt/sources.list.d/xgc2.list

sudo apt-get update
sudo apt-get install ros-noetic-xgc2-swarm-sync-sim
```
