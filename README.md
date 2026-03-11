# Aeisoftware K3s SaaS Platform

Multi-tenant Odoo hosting on K3s HA with automated provisioning, Ceph distributed storage, and Cloudflare routing.

## Architecture

```
Internet → Cloudflare (DNS + Tunnel)
               │
          Traefik Ingress (K3s)
               │
    ┌──────────┼──────────┐
  Odoo 17   Odoo 18   Odoo 19   ...
  client1   client2   client3
    │           │          │
    ├── data  → ceph-rbd  (ReadWriteOnce, Ceph RBD)
    └── addons→ ceph-cephfs (ReadWriteMany, CephFS shared)
               │
    Patroni PostgreSQL HA (3 nodes)
               │
        Ceph Storage Backend
        ├── RBD   → block volumes
        ├── CephFS → shared filesystem
        └── RGW   → S3 API (replaces MinIO)
```

## Infrastructure

| Node | Floating IP | Internal IP | Role |
|:---|:---|:---|:---|
| control-plane-1 | 10.40.2.171 | 10.9.111.28 | K3s Server (etcd) |
| control-plane-2 | 10.40.2.182 | 10.9.111.161 | K3s Server (etcd) |
| control-plane-3 | 10.40.2.153 | 10.9.111.205 | K3s Server (etcd) |
| worker-1 | 10.40.2.158 | — | K3s Agent |
| worker-2 | 10.40.2.159 | — | K3s Agent |
| worker-3 | 10.40.2.156 | — | K3s Agent |
| PostgreSQL-1 | 10.40.2.200 | 10.9.111.157 | Patroni Leader |
| PostgreSQL-2 | 10.40.2.174 | 10.9.111.160 | Patroni Replica |
| PostgreSQL-3 | 10.40.2.193 | 10.9.111.100 | Patroni Replica |
| Ceph (stg-nfs-01) | 10.40.1.240 | — | MON / MDS / RGW |
| Ceph (stg-nfs-02) | 10.40.1.241 | — | MON / MDS / RGW |

## Quick Start — Deploy an Odoo Instance

```bash
export AZURE_PG_HOST="10.9.111.157"    # Patroni primary
export AZURE_PG_USER="odoo"
export AZURE_PG_PASSWORD="<password>"
export CF_API_TOKEN="<cloudflare-token>"
export CF_ACCOUNT_ID="<cf-account>"
export CF_ZONE_ID="<cf-zone>"
export CF_TUNNEL_ID="670c6e18-748b-4399-8fa4-7c78f3a1d342"

# Generate manifests + configure Cloudflare DNS automatically
./crear_instancia_odoo.sh client1 client1.aeisoftware.com 18

# Deploy to K3s
kubectl apply -f k8s-client1/
```

### Generated PVCs (Ceph-backed)
| PVC | StorageClass | Mode | Purpose |
|:---|:---|:---|:---|
| `<name>-odoo-data` | `ceph-rbd` | ReadWriteOnce | Odoo filestore |
| `<name>-odoo-addons` | `ceph-cephfs` | **ReadWriteMany** | Custom addons (shared across all workers) |

### Remove an instance
```bash
kubectl delete ns odoo-<name>
```

## Repository Structure

```
├── crear_instancia_odoo.sh      # Provisioning script
├── cloudflare_provision.py      # Cloudflare automation
├── k3s-cluster/setup-k3s.sh    # K3s HA setup
├── postgresql-ha/setup-patroni.sh
├── ceph/setup-ceph-csi.sh       # Ceph CSI driver setup
├── IMPLEMENTATION_PLAN.md
└── k8s-<client>/                # Generated per-client manifests
```

## Odoo Versions Supported

| Version | Image |
|:---|:---|
| 17 | `odoo:17` |
| 18 | `odoo:18` |
| 19 | `odoo:19` |

## Custom Addons

Copy to the CephFS-backed `addons` PVC — data persists across pod rescheduling:
```bash
kubectl cp ./my_addon odoo-<name>/<pod>:/mnt/extra-addons/
kubectl rollout restart deployment/<name>-odoo -n odoo-<name>
```

## Roadmap

- [ ] HAProxy for Patroni virtual IP
- [ ] Rancher portal
- [ ] Prometheus + Grafana
- [ ] `odoo_k8s_saas` module — auto-provision from E-commerce
- [ ] Azure migration
