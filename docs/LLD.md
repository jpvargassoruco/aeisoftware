# Low-Level Design — Aeisoftware K3s SaaS Platform

> ⚠️ **CONFIDENTIAL** — This document contains passwords, API keys, and infrastructure details. Do not share externally.

---

## 1. Infrastructure Inventory

### 1.1 K3s Cluster Nodes

| Hostname | Floating IP | Internal IP | Role | OS |
|:---|:---|:---|:---|:---|
| control-plane-1 | 10.40.2.171 | 10.9.111.28 | K3s Server (etcd) | Ubuntu 24.04 |
| control-plane-2 | 10.40.2.182 | 10.9.111.161 | K3s Server (etcd) | Ubuntu 24.04 |
| control-plane-3 | 10.40.2.153 | 10.9.111.205 | K3s Server (etcd) | Ubuntu 24.04 |
| worker-1 | 10.40.2.158 | — | K3s Agent | Ubuntu 24.04 |
| worker-2 | 10.40.2.159 | — | K3s Agent | Ubuntu 24.04 |
| worker-3 | 10.40.2.156 | — | K3s Agent | Ubuntu 24.04 |

**K3s Token:** `aeisoftware-k3s-secret`
**Kubeconfig:** `/etc/rancher/k3s/k3s.yaml` on control planes
**Pod CIDRs:** `10.42.0.0/24` through `10.42.5.0/24`
**Service CIDR:** `10.43.0.0/16`

### 1.2 PostgreSQL Cluster Nodes

| Hostname | Floating IP | Internal IP | Role | PostgreSQL |
|:---|:---|:---|:---|:---|
| PostgreSQL-Flexible-1 | 10.40.2.200 | 10.9.111.157 | Patroni Replica | 16.13 |
| PostgreSQL-Flexible-2 | 10.40.2.174 | 10.9.111.160 | Patroni Replica | 16.13 |
| PostgreSQL-Flexible-3 | 10.40.2.193 | 10.9.111.100 | Patroni Leader | 16.13 |

**HAProxy VIP:** `10.9.111.250:5432`
**K8s Service:** `patroni-db.kube-system.svc.cluster.local:5432`
**Cluster Name:** `aeisoftware-pg`

### 1.3 Ceph Storage Nodes

| Hostname | Floating IP | Services |
|:---|:---|:---|
| stg-nfs-01 | 10.40.1.240 | MON / MDS / RGW |
| stg-nfs-02 | 10.40.1.241 | MON / MDS / RGW |

**RGW (S3) Endpoint:** `http://10.40.1.240:7480`
**Ceph Cluster ID:** `99efe072-cf04-11f0-adef-0cc47af94ce2`

---

## 2. Credentials & Secrets

### 2.1 PostgreSQL

| Parameter | Value |
|:---|:---|
| Admin User | `odoo` |
| Admin Password | `Ribentek2026+` |
| Superuser | `postgres` |
| Superuser Password | `Admin2026+` |
| Replication User | `replicator` |
| Replication Password | `replicator_pass` |

### 2.2 Cloudflare

| Parameter | Value |
|:---|:---|
| API Token | `feVXj6XvlWKfhJNNiAFJmaA-MD_4G_iYaXEfY0K_` |
| Account ID | `2755b41b811dc9afc7396ed5d1e27644` |
| Zone ID | `8a6fcaacd01aa1d8e544c85df5a88c8c` |
| Tunnel ID (WSL) | `3829a206-f7ed-4e6a-b75d-954444eaa4a4` |
| Tunnel ID (Portal) | `670c6e18-748b-4399-8fa4-7c78f3a1d342` |
| Domain | `aeisoftware.com` |

### 2.3 SaaS Portal

| Parameter | Value |
|:---|:---|
| API Key | `aei-saas-8700533d55e0ac70ed1385bc` |
| URL | `https://portal.aeisoftware.com` |
| Image | `ghcr.io/jpvargassoruco/aeisoftware/saas-portal:latest` |
| Namespace | `portal-system` |
| Replicas | 2 |
| Port | 8000 |

### 2.4 Ceph / S3 (RGW)

| Parameter | Value |
|:---|:---|
| S3 Endpoint | `http://10.40.1.240:7480` |
| S3 Access Key | `HG6ZI5H7Y9K0GOXYR5GX` |
| S3 Secret Key | `ml7WYjuyurBCLDa6eZJ9zoaNUbmqAdCVW8YxI9Vs` |
| S3 Bucket | `odoo-templates` |
| RBD Key | `AQB7ArFpj37wBRAAC9xs4/vAQ886Z4Oib9dcKg==` |
| CephFS Admin Key | `AQDnFS5pwXuWEhAAI6aoJwDSbdDOmOg5qKLnBg==` |
| RGW User | `aeisoftware` |

### 2.5 SSH Access

| Parameter | Value |
|:---|:---|
| SSH Key | `~/.ssh/id_rsa` (on WSL) |
| Username | `ubuntu` |
| Scope | All K3s and PostgreSQL nodes |
| Note | sudo requires password on PG nodes |

### 2.6 GitHub / GHCR

| Parameter | Value |
|:---|:---|
| Repository | `jpvargassoruco/aeisoftware` |
| Branch | `k3s-saas` |
| Registry | `ghcr.io/jpvargassoruco/aeisoftware/saas-portal` |
| Image Pull Secret | `ghcr-credentials` (in `portal-system` namespace) |

---

## 3. K3s Cluster Configuration

### 3.1 Installation
```bash
# First control plane (init)
curl -sfL https://get.k3s.io | K3S_TOKEN="aeisoftware-k3s-secret" \
  INSTALL_K3S_EXEC="server --cluster-init --node-ip=10.9.111.28 \
  --advertise-address=10.9.111.28 --disable=traefik \
  --tls-san=10.9.111.28 --tls-san=10.40.2.171 ..." sh -

# Additional control planes (join)
curl -sfL https://get.k3s.io | K3S_TOKEN="aeisoftware-k3s-secret" \
  INSTALL_K3S_EXEC="server --server https://10.9.111.28:6443 ..." sh -

# Workers
curl -sfL https://get.k3s.io | K3S_TOKEN="aeisoftware-k3s-secret" \
  K3S_URL="https://10.9.111.28:6443" \
  INSTALL_K3S_EXEC="agent --node-ip=<IP>" sh -
```

### 3.2 Namespaces
| Namespace | Purpose |
|:---|:---|
| `kube-system` | HAProxy for Patroni, system services |
| `portal-system` | SaaS Portal deployment |
| `ceph-csi` | Ceph RBD CSI driver |
| `ceph-csi-cephfs` | Ceph CephFS CSI driver |
| `odoo-{name}` | Per-tenant Odoo instance |

### 3.3 StorageClasses
| Name | Provisioner | Access Mode | Use |
|:---|:---|:---|:---|
| `ceph-rbd` (default) | `rbd.csi.ceph.com` | ReadWriteOnce | Odoo data/filestore |
| `ceph-cephfs` | `cephfs.csi.ceph.com` | ReadWriteMany | Shared addons |

### 3.4 Ingress
- **Controller:** Traefik (custom install, not K3s default)
- **Entrypoint:** `web` (HTTP)
- **Portal rule:** `host: portal.aeisoftware.com` → `saas-portal-svc:8000`
- **Odoo rules:** `host: {name}.aeisoftware.com` → `{name}-odoo-svc:8069`

---

## 4. Patroni PostgreSQL Configuration

### 4.1 Patroni Config (`/etc/patroni/config.yml`)
```yaml
scope: aeisoftware-pg
namespace: /patroni/
restapi:
  listen: 0.0.0.0:8008
  connect_address: <NODE_IP>:8008
etcd3:
  hosts: 10.9.111.157:2379,10.9.111.160:2379,10.9.111.100:2379
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
postgresql:
  listen: 0.0.0.0:5432
  data_dir: /var/lib/postgresql/16/main
  bin_dir: /usr/lib/postgresql/16/bin
```

### 4.2 pg_hba.conf
```
# Default rules
local   all        all                  trust
host    all        all  127.0.0.1/32    trust

# Patroni-managed
host replication replicator 0.0.0.0/0     md5
host all         odoo        0.0.0.0/0     md5
host all         all         127.0.0.1/32  trust

# K3s access (added via Patroni DCS PATCH)
host all         all         10.9.111.0/24 md5
host all         all         10.42.0.0/16  md5

# Per-instance roles connect via the same K3s/pod subnets
# Each instance uses odoo_{name} role (auto-created by portal)
```

### 4.3 Administration Commands
```bash
# SSH to PG leader
ssh -i ~/.ssh/id_rsa ubuntu@10.40.2.193

# Patroni cluster status
sudo patronictl -c /etc/patroni/config.yml list

# Show DCS config
sudo patronictl -c /etc/patroni/config.yml show-config

# Manual switchover
sudo patronictl -c /etc/patroni/config.yml switchover

# PostgreSQL superuser access
sudo -u postgres psql

# List databases
sudo -u postgres psql -c "\l"

# Drop a database
sudo -u postgres psql -c 'DROP DATABASE "dbname";'

# Patroni REST API (from K3s CP only, internal network)
ssh -i ~/.ssh/id_rsa ubuntu@10.40.2.171
curl http://10.9.111.100:8008/config        # GET config
curl -X PATCH http://10.9.111.100:8008/config -H 'Content-Type: application/json' -d '{...}'
curl -X POST http://10.9.111.100:8008/reload  # reload pg_hba
```

### 4.4 Services
```bash
# On PG nodes:
sudo systemctl status patroni     # Patroni HA manager
sudo systemctl status etcd        # DCS backend
# Note: postgresql.service is DISABLED — Patroni manages PostgreSQL directly
```

---

## 5. SaaS Portal API

### 5.1 Authentication
```bash
curl -H "X-API-Key: aei-saas-8700533d55e0ac70ed1385bc" https://portal.aeisoftware.com/api/...
```

### 5.2 Endpoints

| Method | Endpoint | Description |
|:---|:---|:---|
| `GET` | `/api/instances` | List instances (paginated: `?page=1&page_size=50`) |
| `POST` | `/api/instances` | Create new Odoo instance |
| `GET` | `/api/instances/{name}` | Get instance details |
| `DELETE` | `/api/instances/{name}` | Delete instance + Cloudflare cleanup |
| `GET` | `/api/instances/{name}/config` | Get raw odoo.conf |
| `PATCH` | `/api/instances/{name}/config` | Update odoo.conf overrides or addons repos |
| `PUT` | `/api/instances/{name}/config` | Replace entire odoo.conf |
| `POST` | `/api/instances/{name}/restart` | Rolling restart |
| `GET` | `/api/instances/{name}/logs` | Tail pod logs |
| `GET` | `/api/instances/{name}/addons-usage` | Disk usage (max-depth 2) |
| `PATCH` | `/api/instances/{name}/protect` | Toggle delete protection |
| `GET` | `/api/templates` | List DB templates in S3 |
| `POST` | `/api/templates/{path}` | Upload pg_dump to S3 |
| `DELETE` | `/api/templates/{path}` | Delete template from S3 |
| `GET` | `/health` | Health check |

### 5.3 Create Instance Request
```json
{
  "name": "acme",
  "domain": "acme.aeisoftware.com",
  "odoo_version": "18",
  "db_password": "master_password_for_admin_panel",
  "db_template": "v18/starter.dump",
  "image": null,
  "addons_repos": [
    {"url": "https://github.com/org/addons.git", "branch": "18.0"}
  ],
  "odoo_conf_overrides": {"workers": 4}
}
```

### 5.4 Generated odoo.conf
```ini
[options]
db_host = patroni-db.kube-system.svc.cluster.local
db_port = 5432
db_user = odoo_acme
db_password = <random-32-char-password>
db_name = False
db_filter = ^acme$
admin_passwd = master_password_for_admin_panel
list_db = True
addons_path = /mnt/extra-addons,/mnt/extra-addons/addons,/usr/lib/python3/dist-packages/odoo/addons
data_dir = /var/lib/odoo
workers = 4
proxy_mode = True
```

### 5.5 K8s Resources Created Per Instance
| Resource | Name | Namespace |
|:---|:---|:---|
| Namespace | `odoo-{name}` | — |
| LimitRange | `{name}-limits` | `odoo-{name}` |
| ResourceQuota | `{name}-quota` | `odoo-{name}` |
| PodDisruptionBudget | `{name}-pdb` | `odoo-{name}` |
| Secret | `{name}-db-secret` | `odoo-{name}` |
| ConfigMap | `{name}-odoo-conf` | `odoo-{name}` |
| PVC (data) | `{name}-data` | `odoo-{name}` |
| PVC (addons) | `{name}-addons` | `odoo-{name}` |
| Deployment | `{name}-odoo` | `odoo-{name}` |
| Service | `{name}-odoo-svc` | `odoo-{name}` |
| Ingress | `{name}-odoo-ingress` | `odoo-{name}` |

### 5.6 Init Containers
1. **setup-db** (`postgres:16-alpine`): Creates the per-instance PostgreSQL database if it doesn't exist. Optionally restores from a DB template via S3. After DB init, the portal transfers ownership to the per-instance PG role.
2. **sync-addons** (`alpine/git`): Clones/pulls configured git repos into `/mnt/extra-addons/{repo-name}/`.

### 5.7 Portal K8s Deployment
```yaml
Namespace: portal-system
ServiceAccount: saas-portal
ClusterRole: saas-portal-manager
Replicas: 2
Image: ghcr.io/jpvargassoruco/aeisoftware/saas-portal:latest
Port: 8000
Probes: readiness on /health (5s initial, 10s period)
Resources: 100m-500m CPU, 128Mi-512Mi memory
```

---

## 6. Cloudflare Integration

### 6.1 Tunnel Architecture
```
Internet → Cloudflare Edge → Tunnel → cloudflared (Docker/WSL) → K3s Traefik → Odoo/Portal
```

### 6.2 Tunnel Client
```yaml
# cloudflare-tunnel/docker-compose.yml
services:
  cloudflare-tunnel:
    image: cloudflare/cloudflared:latest
    command: tunnel --no-autoupdate run --token <JWT>
    networks: [web-proxy]
```

### 6.3 DNS Route Creation (Portal)
On instance creation, the portal calls:
1. `PUT /zones/{zone_id}/dns_records` — Creates `{name}.aeisoftware.com` CNAME → `{tunnel_id}.cfargotunnel.com`
2. `PUT /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations` — Adds ingress rule `{name}.aeisoftware.com` → `http://traefik.kube-system.svc.cluster.local:80`

On deletion, the portal reverses both operations.

---

## 7. Ceph CSI Configuration

### 7.1 Pools
| Pool | Type | Use |
|:---|:---|:---|
| `k3s-rbd` | RBD (block) | Odoo data PVCs |
| `k3s-cephfs-meta` | CephFS metadata | CephFS metadata |
| `k3s-cephfs-data` | CephFS data | Shared addons PVCs |

### 7.2 CephFS
- **Filesystem:** `k3s-cephfs`
- **Subvolume group:** `csi` (required by CSI provisioner)
- **MDS:** 2 instances on stg-nfs-01 and stg-nfs-02

### 7.3 RGW (S3)
- **User:** `aeisoftware`
- **Bucket:** `odoo-templates`
- **Use:** Store and retrieve pg_dump database templates

---

## 8. CI/CD Pipeline

### 8.1 GitHub Actions (`.github/workflows/build-portal.yaml`)
**Trigger:** Push to `k3s-saas` branch (files in `portal/`)
**Steps:**
1. Build `portal/Dockerfile`
2. Push to `ghcr.io/jpvargassoruco/aeisoftware/saas-portal:latest`

**Required Setting:** Repository → Settings → Actions → General → Workflow permissions → "Read and write permissions"

### 8.2 Deployment
```bash
# After CI builds:
kubectl rollout restart deployment/saas-portal -n portal-system
kubectl rollout status deployment/saas-portal -n portal-system --timeout=120s
```

---

## 9. Operational Procedures

### 9.1 Create Instance (Portal UI)
1. Open `https://portal.aeisoftware.com`
2. Click "Create Instance"
3. Fill: Name, Domain, Version, Master Password, optional addons repos
4. Click Create → Instance provisions automatically

### 9.2 Delete Instance
1. Portal UI: Click trash icon → Confirm
2. Removes: K8s namespace, Cloudflare route, database (via portal)

### 9.3 Manage Addons
1. Portal UI: Config → Addon Repos tab
2. Add repo URL + branch → Save
3. Deployment restarts, sync-addons initContainer clones the repo

### 9.4 Edit odoo.conf
1. Portal UI: Config → odoo.conf tab
2. Edit the configuration text
3. Save & Restart → ConfigMap updated, deployment restarted

### 9.5 Database Templates
1. Create a template: `pg_dump -h patroni-db... -U odoo -Fc mydb > template.dump`
2. Upload via portal: Templates section → Upload
3. Use on create: Select template in the create dialog

### 9.6 Monitoring
```bash
# Portal logs
kubectl logs -n portal-system deploy/saas-portal -f --tail=50

# Instance logs
kubectl logs -n odoo-{name} deploy/{name}-odoo -f --tail=50

# Patroni cluster status
ssh -i ~/.ssh/id_rsa ubuntu@10.40.2.193 "sudo patronictl -c /etc/patroni/config.yml list"

# K3s cluster status
kubectl get nodes -o wide
kubectl get pods -A | grep -v Completed
```

---

## 10. Network Diagram

```
WSL (ubuntu@ASUS-JPVS) ──SSH──► K3s Control Planes (10.40.2.171/182/153)
         │                                │
         │──SSH──► PG Nodes (10.40.2.200/174/193)
         │                                │
         ├──Docker──► cloudflared ──Tunnel──► Cloudflare Edge
         │                                    │
         │                             *.aeisoftware.com
         │                                    │
         └──kubectl──► K3s API ──────► Traefik Ingress
                           │                  │
                           │        ┌─────────┴─────────┐
                           │        │ portal.aei...      │ {name}.aei...
                           │        │ :8000              │ :8069
                           │        │ saas-portal        │ odoo pods
                           │        └────────────────────┘
                           │                  │
                           └──────► patroni-db.kube-system:5432
                                          │ (HAProxy VIP)
                                   ┌──────┼──────┐
                                   PG-1  PG-3   PG-2
                                   .157  .100   .160
                                   (R)   (L)    (R)
```
