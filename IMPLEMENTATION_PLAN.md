# Aeisoftware K3s SaaS Platform — Implementation Plan

## Deployed Stack ✅

| Component | Details |
|:---|:---|
| K3s HA | 3 CP (etcd) + 3 workers |
| Traefik + Cloudflare | v3.6.10, Tunnel 670c6e18 |
| Patroni PostgreSQL | Leader + 2 streaming replicas, lag=0 |
| **Ceph RBD** (`ceph-rbd`) | ReadWriteOnce — Odoo data |
| **Ceph CephFS** (`ceph-cephfs`) | ReadWriteMany — addons shared across all workers |
| **Ceph RGW** | S3 API — replaces MinIO |
| Odoo clients | client1 (v17), client2 (v18), client3 (v19) |

---

## Phase 6: Production Hardening (Next)

### HAProxy for Patroni Virtual IP
Single DB write endpoint that always routes to the current Patroni leader.  
Deploy on one control-plane node.

### Rancher Portal
```bash
helm install rancher rancher-latest/rancher -n cattle-system \
  --set hostname=rancher.aeisoftware.com
```

### Monitoring
`kube-prometheus-stack` Helm chart → Prometheus + Grafana.

---

## Future: `odoo_k8s_saas` Module

Customer buys a plan on Odoo E-commerce → Odoo module auto-provisions:
1. K3s namespace + Deployment
2. Ceph PVCs (data: `ceph-rbd`, addons: `ceph-cephfs`)
3. Cloudflare DNS + Tunnel route
4. Manages lifecycle: active → suspended → deleted

---

## Azure Migration Path

> [!NOTE]
> Pod manifests and `crear_instancia_odoo.sh` are cloud-agnostic.  
> Only StorageClass names change.

| Current (OpenStack/Ceph) | Azure Equivalent |
|:---|:---|
| `ceph-rbd` | Azure Disk (managed-premium) |
| `ceph-cephfs` | Azure Files (NFS) |
| Ceph RGW | Azure Blob Storage |
| Patroni | Azure Database for PostgreSQL Flexible |
