#!/bin/sh
# Firewall: SMTP (25) and HTTPS (443) from anywhere; SSH only from SSH_FROM (PLAN Q33).
# Docker publishes ports through its own iptables chains; the compose file publishes only 25 and
# 443, so the exposed surface matches this policy.
set -eu
: "${SSH_FROM:?set SSH_FROM to the IP or CIDR allowed to use SSH}"
ufw default deny incoming
ufw default allow outgoing
ufw allow 25/tcp
ufw allow 443/tcp
ufw allow from "$SSH_FROM" to any port 22 proto tcp
ufw --force enable
ufw status verbose
