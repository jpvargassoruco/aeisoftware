#!/bin/bash
# Install PostgreSQL 16 + Patroni (pip) + etcd (binary) on Ubuntu 24.04
# Usage: bash setup-patroni.sh <node_name> <node_ip> <pg1_ip> <pg2_ip> <pg3_ip>

NODE_NAME=$1
NODE_IP=$2
PG1_IP=$3
PG2_IP=$4
PG3_IP=$5
ETCD_VER="v3.5.17"

set -e
echo "[*] Setting up $NODE_NAME ($NODE_IP)..."

# Install PostgreSQL and pip dependencies
sudo apt-get update -qq
sudo apt-get install -y -qq postgresql-16 python3-pip python3-psycopg2 python3-dev libpq-dev
sudo pip3 install patroni[etcd3] --break-system-packages -q

# Stop postgresql so Patroni manages it
sudo systemctl stop postgresql || true
sudo systemctl disable postgresql || true

# Install etcd binary
if [ ! -f /usr/local/bin/etcd ]; then
    wget -q "https://github.com/etcd-io/etcd/releases/download/${ETCD_VER}/etcd-${ETCD_VER}-linux-amd64.tar.gz" -O /tmp/etcd.tar.gz
    tar xzf /tmp/etcd.tar.gz -C /tmp/
    sudo cp /tmp/etcd-${ETCD_VER}-linux-amd64/etcd /usr/local/bin/
    sudo cp /tmp/etcd-${ETCD_VER}-linux-amd64/etcdctl /usr/local/bin/
fi

# Configure etcd
sudo mkdir -p /etc/etcd /var/lib/etcd
sudo tee /etc/etcd/etcd.conf.yml > /dev/null << ETCDEOF
name: '${NODE_NAME}'
data-dir: /var/lib/etcd
listen-peer-urls: http://${NODE_IP}:2380
listen-client-urls: http://${NODE_IP}:2379,http://127.0.0.1:2379
advertise-client-urls: http://${NODE_IP}:2379
initial-advertise-peer-urls: http://${NODE_IP}:2380
initial-cluster: PostgreSQL-Flexible-1=http://${PG1_IP}:2380,PostgreSQL-Flexible-2=http://${PG2_IP}:2380,PostgreSQL-Flexible-3=http://${PG3_IP}:2380
initial-cluster-token: 'aeisoftware-etcd'
initial-cluster-state: 'new'
ETCDEOF

# etcd systemd service
sudo tee /etc/systemd/system/etcd.service > /dev/null << SVCEOF
[Unit]
Description=etcd key-value store
After=network.target

[Service]
ExecStart=/usr/local/bin/etcd --config-file /etc/etcd/etcd.conf.yml
Restart=always
RestartSec=5
LimitNOFILE=40000

[Install]
WantedBy=multi-user.target
SVCEOF

# Configure Patroni
sudo mkdir -p /etc/patroni
sudo tee /etc/patroni/config.yml > /dev/null << PATEOF
scope: aeisoftware-pg
namespace: /patroni/
name: ${NODE_NAME}

restapi:
  listen: 0.0.0.0:8008
  connect_address: ${NODE_IP}:8008

etcd3:
  hosts: ${PG1_IP}:2379,${PG2_IP}:2379,${PG3_IP}:2379

bootstrap:
  dcs:
    ttl: 30
    loop_wait: 10
    retry_timeout: 10
    maximum_lag_on_failover: 1048576
    postgresql:
      use_pg_rewind: true
      parameters:
        max_connections: 200
        shared_buffers: 256MB
        wal_level: replica
        hot_standby: 'on'
        wal_log_hints: 'on'
  initdb:
    - encoding: UTF8
    - data-checksums
  pg_hba:
    - host replication replicator 0.0.0.0/0 md5
    - host all odoo 0.0.0.0/0 md5
    - host all all 127.0.0.1/32 trust

postgresql:
  listen: 0.0.0.0:5432
  connect_address: ${NODE_IP}:5432
  data_dir: /var/lib/postgresql/16/main
  bin_dir: /usr/lib/postgresql/16/bin
  authentication:
    replication:
      username: replicator
      password: replicator_pass
    superuser:
      username: postgres
      password: Admin2026+
    rewind:
      username: postgres
  parameters:
    unix_socket_directories: '/var/run/postgresql'

tags:
  nofailover: false
  noloadbalance: false
  clonefrom: false
  nosync: false
PATEOF

# Patroni systemd service
sudo tee /etc/systemd/system/patroni.service > /dev/null << PATSVCEOF
[Unit]
Description=Patroni PostgreSQL HA
After=network.target etcd.service

[Service]
User=postgres
ExecStart=/usr/local/bin/patroni /etc/patroni/config.yml
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
PATSVCEOF

sudo chown postgres:postgres /etc/patroni/config.yml
sudo chown -R postgres:postgres /var/lib/postgresql/

# Start etcd first
sudo rm -rf /var/lib/etcd/*
sudo systemctl daemon-reload
sudo systemctl enable etcd
sudo systemctl start etcd

echo "[*] etcd started on $NODE_NAME"
echo "[*] Starting Patroni on $NODE_NAME..."
sudo systemctl enable patroni
sudo systemctl start patroni

sleep 3
echo "[*] Status on $NODE_NAME:"
sudo systemctl is-active etcd && echo "etcd: OK" || echo "etcd: FAILED"
sudo systemctl is-active patroni && echo "patroni: OK" || echo "patroni: FAILED"
