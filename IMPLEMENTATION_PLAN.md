# Aeisoftware K3s SaaS Platform — Implementation Plan

## Current State ✅

| Component | Status |
|:---|:---|
| K3s HA (3 CP + 3 workers) | Running |
| Traefik v3.6.10 | Running (NodePort 30080) |
| Cloudflare Tunnel | Connected |
| Patroni PostgreSQL HA | Leader + 2 streaming replicas |
| MinIO (temporary) | Single node — **to be replaced** |
| client1 Odoo 17 | HTTP 303 ✅ |
| client2 Odoo 18 | HTTP 303 ✅ |
| client3 Odoo 19 | HTTP 303 ✅ |

---

## Phase 5: Ceph Distributed Storage (Next)

> [!IMPORTANT]
> MinIO is a **single point of failure**. The OpenStack cluster uses **Ceph** as its storage backend — we can consume it natively for fully HA storage.

### Option A: OpenStack Cinder CSI *(Recommended)*
Install the OpenStack Cloud Controller + Cinder CSI plugin. K3s provisions Ceph-backed volumes through the OpenStack API automatically.

**Pros**: No direct Ceph config, easiest to set up, same volumes as OpenStack VMs  
**Cons**: ReadWriteMany requires CephFS configured separately

### Option B: Ceph CSI Driver *(More control)*
Install `ceph-csi` pointing directly at the Ceph cluster. Two StorageClasses:
- `ceph-rbd` → ReadWriteOnce (Odoo data, Patroni WAL)
- `ceph-cephfs` → **ReadWriteMany** (shared addons across all workers)

Replace MinIO with **Ceph RADOS Gateway (RGW)** — S3-compatible, already part of Ceph, fully HA.

### Questions before starting
- [ ] Can we SSH or get credentials to the OpenStack Ceph admin?
- [ ] Is the Ceph RADOS Gateway (RGW) already enabled on the cluster?
- [ ] OpenStack Keystone credentials for Cinder CSI

---

## Phase 6: Production Hardening

- HAProxy + keepalived for Patroni virtual IP (single write endpoint)
- Rancher portal for cluster management
- Prometheus + Grafana monitoring
- Azure migration: swap StorageClasses + managed PG → minimal downtime

---

## Final Architecture Target

```
Cloudflare (DNS + Tunnel)
       │
 Traefik Ingress
       │
  Odoo Pods (workers)
  ├── filestore → Ceph RBD PVC (RWO, per client)
  └── addons   → Ceph CephFS PVC (RWM, shared)
       │
 Patroni PG HA ← HAProxy virtual IP
       │
 Ceph Cluster (OpenStack backend)
 ├── RBD  → block storage (data)
 ├── CephFS → shared filesystem (addons)
 └── RGW  → S3 API (replaces MinIO)
```
