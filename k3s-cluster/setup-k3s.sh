#!/bin/bash
# K3s HA Cluster Setup Script for OpenStack
# Run this on the FIRST control-plane node to initialize, then on others to join.
# Usage:
#   First node:      bash setup-k3s.sh init   10.9.111.28
#   Additional CPs:  bash setup-k3s.sh server 10.9.111.161 10.9.111.28
#   Workers:         bash setup-k3s.sh agent  10.9.111.58  10.9.111.28

MODE=$1      # init | server | agent
NODE_IP=$2   # this node's internal IP
INIT_IP=$3   # first control-plane IP (for join)
TOKEN="aeisoftware-k3s-secret"
TLS_SANS="--tls-san=10.9.111.28 --tls-san=10.40.2.171 --tls-san=10.9.111.161 --tls-san=10.40.2.182 --tls-san=10.9.111.205 --tls-san=10.40.2.153"

case "$MODE" in
  init)
    echo "[*] Initializing first K3s control-plane on $NODE_IP..."
    curl -sfL https://get.k3s.io | K3S_TOKEN="$TOKEN" \
      INSTALL_K3S_EXEC="server --cluster-init --node-ip=$NODE_IP --advertise-address=$NODE_IP $TLS_SANS --disable=traefik" \
      sh -
    echo "[✓] First control-plane ready. Token: $TOKEN"
    ;;
  server)
    echo "[*] Joining $NODE_IP as K3s control-plane (cluster at $INIT_IP)..."
    curl -sfL https://get.k3s.io | K3S_TOKEN="$TOKEN" \
      INSTALL_K3S_EXEC="server --server https://$INIT_IP:6443 --node-ip=$NODE_IP --advertise-address=$NODE_IP $TLS_SANS --disable=traefik" \
      sh -
    echo "[✓] Control-plane joined."
    ;;
  agent)
    echo "[*] Joining $NODE_IP as K3s worker (cluster at $INIT_IP)..."
    curl -sfL https://get.k3s.io | K3S_TOKEN="$TOKEN" K3S_URL="https://$INIT_IP:6443" \
      INSTALL_K3S_EXEC="agent --node-ip=$NODE_IP" \
      sh -
    echo "[✓] Worker joined."
    ;;
  *)
    echo "Usage: $0 <init|server|agent> <node_ip> [init_ip]"
    exit 1
    ;;
esac
