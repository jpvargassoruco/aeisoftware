# Aeisoftware K3s SaaS Platform

Production-grade Odoo multi-tenant hosting on a K3s HA cluster with automated provisioning and Cloudflare routing.

## Architecture

```
Internet → Cloudflare (DNS + Tunnel)
               │
          Traefik Ingress (K3s)
               │
    ┌──────────┼──────────┐
  Odoo 17   Odoo 18   Odoo 19
  client1   client2   client3
    └──────────┼──────────┘
               │
    Patroni PostgreSQL HA (3 nodes)
               │
        MinIO S3 Storage
```

## Infrastructure

| Node | IP | Role |
|:---|:---|:---|
| control-plane-1 | 10.40.2.171 | K3s Server (etcd) |
| control-plane-2 | 10.40.2.182 | K3s Server (etcd) |
| control-plane-3 | 10.40.2.153 | K3s Server (etcd) |
| worker-1 | 10.40.2.158 | K3s Agent (Odoo pods) |
| worker-2 | 10.40.2.159 | K3s Agent (Odoo pods) |
| worker-3 | 10.40.2.156 | K3s Agent (Odoo pods) |
| PostgreSQL-Flexible-1 | 10.40.2.200 | Patroni Leader |
| PostgreSQL-Flexible-2 | 10.40.2.174 | Patroni Replica |
| PostgreSQL-Flexible-3 | 10.40.2.193 | Patroni Replica |
| Blob-Storage | 10.40.2.190 | MinIO S3 |

## Quick Start

### Prerequisites

```bash
export AZURE_PG_HOST="<patroni-primary-ip>"
export AZURE_PG_USER="odoo"
export AZURE_PG_PASSWORD="<password>"
export CF_API_TOKEN="<cloudflare-token>"
export CF_ACCOUNT_ID="<cloudflare-account>"
export CF_ZONE_ID="<cloudflare-zone>"
export CF_TUNNEL_ID="<cloudflare-tunnel>"
```

### Deploy a new Odoo client

```bash
# Usage: ./crear_instancia_odoo.sh <name> <domain> <odoo_version>
./crear_instancia_odoo.sh multipago multipago.sdnbo.net 18

# Apply to K3s
kubectl apply -f k8s-multipago/
```

### Remove a client

```bash
kubectl delete ns odoo-<name>
```

## Repository Structure

```
├── crear_instancia_odoo.sh     # Main provisioning script (K8s manifests + Cloudflare)
├── cloudflare_provision.py     # Cloudflare API automation
├── cloudflare_manager/         # Cloudflare API client
├── k3s-cluster/
│   └── setup-k3s.sh           # K3s HA setup (init / server / agent)
├── postgresql-ha/
│   └── setup-patroni.sh       # Patroni + etcd HA setup
├── minio/
│   └── setup-minio.sh         # MinIO S3 setup
└── k8s-<client>/              # Generated manifests per client
    ├── 00-namespace.yaml
    ├── 01-secret.yaml
    ├── 02-configmap.yaml       # odoo.conf
    ├── 03-pvc.yaml
    ├── 04-deployment.yaml
    ├── 05-service.yaml
    └── 06-ingress.yaml
```

## Odoo Versions Supported

| Version | Docker Image | PostgreSQL Support |
|:---|:---|:---|
| 17 | `odoo:17` | PG 12–16 (PG 17 works) |
| 18 | `odoo:18` | PG 14–17 ✅ |
| 19 | `odoo:19` | PG 15–17+ ✅ |

## Custom Addons

Copy addons to the worker node, then into the running pod:

```bash
# Copy to VM
scp -r ./my_addon ubuntu@<worker-ip>:/tmp/

# Copy into pod
kubectl cp /tmp/my_addon odoo-<client>/<pod-name>:/mnt/extra-addons/

# Restart pod to pick up new modules
kubectl rollout restart deployment/<client>-odoo -n odoo-<client>
```

> **Note**: Addon persistence across pod rescheduling requires shared storage (Ceph CephFS or NFS). See implementation plan.

## Roadmap

- [ ] Ceph CSI driver for distributed storage (replaces MinIO + NFS)
- [ ] HAProxy for Patroni virtual IP
- [ ] Rancher portal
- [ ] `odoo_k8s_saas` module — auto-provision instances from Odoo E-commerce
- [ ] Azure migration
