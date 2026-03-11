#!/bin/bash
# MinIO S3 Setup Script
# Usage: bash setup-minio.sh
# Run on the Blob-Storage node

MINIO_USER="aeisoftware"
MINIO_PASS="Ribentek2026+"
DATA_DIR="/data/minio"

echo "[*] Installing MinIO..."
wget -q https://dl.min.io/server/minio/release/linux-amd64/minio -O /tmp/minio
sudo install /tmp/minio /usr/local/bin/minio

sudo mkdir -p $DATA_DIR
sudo useradd -r minio-user -s /sbin/nologin 2>/dev/null || true
sudo chown minio-user:minio-user $DATA_DIR

sudo tee /etc/systemd/system/minio.service > /dev/null << EOF
[Unit]
Description=MinIO S3 Compatible Storage
After=network-online.target

[Service]
User=minio-user
Group=minio-user
ExecStart=/usr/local/bin/minio server ${DATA_DIR} --console-address :9001
Environment=MINIO_ROOT_USER=${MINIO_USER}
Environment=MINIO_ROOT_PASSWORD=${MINIO_PASS}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable minio
sudo systemctl start minio

echo "[✓] MinIO installed!"
echo "    API:     http://$(hostname -I | awk '{print $1}'):9000"
echo "    Console: http://$(hostname -I | awk '{print $1}'):9001"
echo "    User:    $MINIO_USER"
