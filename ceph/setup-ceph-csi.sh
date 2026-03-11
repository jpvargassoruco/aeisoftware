#!/bin/bash
# Ceph CSI driver setup for K3s
# Run from WSL with kubectl configured to the K3s cluster
# Requires: helm installed on control-plane-1 (via SSH)

# =========================================================
# CEPH CLUSTER INFO — UPDATE THESE VALUES
# =========================================================
CLUSTER_ID="99efe072-cf04-11f0-adef-0cc47af94ce2"
MON1="10.40.1.240:6789"
MON2="10.40.1.241:6789"
RBD_KEY="AQB7ArFpj37wBRAAC9xs4/vAQ886Z4Oib9dcKg=="
CEPHFS_KEY="AQB7ArFpT7oHKRAA2kjbeyD28qAWBHuozdYM+g=="
RGW_ENDPOINT="http://10.40.1.240:7480"
RGW_ACCESS_KEY="aeisoftware"
RGW_SECRET_KEY="Ribentek2026+"
# =========================================================

set -e
CP1="10.40.2.171"
SSH_KEY="/home/ubuntu/.ssh/id_rsa"

echo "[*] Adding Ceph CSI Helm repo..."
ssh -i $SSH_KEY ubuntu@$CP1 "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml && sudo -E helm repo add ceph-csi https://ceph.github.io/csi-charts && sudo -E helm repo update"

echo "[*] Creating namespaces and secrets..."
kubectl create namespace ceph-csi 2>/dev/null || true
kubectl create namespace ceph-csi-cephfs 2>/dev/null || true

kubectl apply -f - <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: csi-rbd-secret
  namespace: ceph-csi
stringData:
  userID: k3s-rbd
  userKey: ${RBD_KEY}
---
apiVersion: v1
kind: Secret
metadata:
  name: csi-cephfs-secret
  namespace: ceph-csi-cephfs
stringData:
  adminID: k3s-cephfs
  adminKey: ${CEPHFS_KEY}
  userID: k3s-cephfs
  userKey: ${CEPHFS_KEY}
EOF

echo "[*] Installing ceph-csi-rbd..."
ssh -i $SSH_KEY ubuntu@$CP1 "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml && sudo -E helm install ceph-csi-rbd ceph-csi/ceph-csi-rbd \
  --namespace ceph-csi \
  --set csiConfig[0].clusterID='${CLUSTER_ID}' \
  --set 'csiConfig[0].monitors[0]=${MON1}' \
  --set 'csiConfig[0].monitors[1]=${MON2}' \
  --set secret.create=false --set storageClass.create=false 2>&1"

echo "[*] Installing ceph-csi-cephfs..."
ssh -i $SSH_KEY ubuntu@$CP1 "export KUBECONFIG=/etc/rancher/k3s/k3s.yaml && sudo -E helm install ceph-csi-cephfs ceph-csi/ceph-csi-cephfs \
  --namespace ceph-csi-cephfs \
  --set csiConfig[0].clusterID='${CLUSTER_ID}' \
  --set 'csiConfig[0].monitors[0]=${MON1}' \
  --set 'csiConfig[0].monitors[1]=${MON2}' \
  --set secret.create=false --set storageClass.create=false 2>&1"

echo "[*] Creating StorageClasses..."
kubectl apply -f - <<EOF
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ceph-rbd
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: rbd.csi.ceph.com
parameters:
  clusterID: "${CLUSTER_ID}"
  pool: "k3s-rbd"
  imageFeatures: layering
  csi.storage.k8s.io/provisioner-secret-name: csi-rbd-secret
  csi.storage.k8s.io/provisioner-secret-namespace: ceph-csi
  csi.storage.k8s.io/controller-expand-secret-name: csi-rbd-secret
  csi.storage.k8s.io/controller-expand-secret-namespace: ceph-csi
  csi.storage.k8s.io/node-stage-secret-name: csi-rbd-secret
  csi.storage.k8s.io/node-stage-secret-namespace: ceph-csi
reclaimPolicy: Retain
allowVolumeExpansion: true
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: ceph-cephfs
provisioner: cephfs.csi.ceph.com
parameters:
  clusterID: "${CLUSTER_ID}"
  fsName: "k3s-cephfs"
  csi.storage.k8s.io/provisioner-secret-name: csi-cephfs-secret
  csi.storage.k8s.io/provisioner-secret-namespace: ceph-csi-cephfs
  csi.storage.k8s.io/controller-expand-secret-name: csi-cephfs-secret
  csi.storage.k8s.io/controller-expand-secret-namespace: ceph-csi-cephfs
  csi.storage.k8s.io/node-stage-secret-name: csi-cephfs-secret
  csi.storage.k8s.io/node-stage-secret-namespace: ceph-csi-cephfs
reclaimPolicy: Retain
allowVolumeExpansion: true
EOF

echo ""
echo "[✓] Ceph CSI setup complete!"
echo "    StorageClasses:"
kubectl get storageclass
echo ""
echo "    RGW (S3) endpoint: ${RGW_ENDPOINT}"
echo "    S3 access key:     ${RGW_ACCESS_KEY}"
echo "    S3 secret key:     ${RGW_SECRET_KEY}"
