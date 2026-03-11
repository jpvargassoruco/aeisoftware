# Aeisoftware K3s SaaS Platform

Multi-tenant Odoo hosting on K3s HA with automated provisioning, Ceph distributed storage, Cloudflare routing, and a self-hosted SaaS management portal.

## Architecture

```
Internet → Cloudflare (DNS + Tunnel + Access MFA)
               │
          Traefik Ingress (K3s)
               │
    ┌──────────┼────────────┬────────────────────┐
  Odoo 17   Odoo 18   Odoo 19    SaaS Portal
  client1   client2   client3  portal.aeisoftware.com
    │           │          │         │
    ├── data  → ceph-cephfs (ReadWriteMany)    ← K8s Python API
    └── addons→ ceph-cephfs (ReadWriteMany)    ←  ↑ provisions
               │
    HAProxy VIP → patroni-db.kube-system:5432
               │
    Patroni PostgreSQL HA (PG3 Leader, PG1+PG2 Replica)
               │
        Ceph Storage Backend
        ├── CephFS → shared filesystem (data + addons)
        └── RGW   → S3 API (DB templates at s3://odoo-templates/)
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
| PostgreSQL-1 | 10.40.2.200 | 10.9.111.157 | Patroni Replica |
| PostgreSQL-2 | 10.40.2.174 | 10.9.111.160 | Patroni Replica |
| PostgreSQL-3 | 10.40.2.193 | 10.9.111.100 | Patroni Leader |
| Ceph (stg-nfs-01) | 10.40.1.240 | — | MON / MDS / RGW |
| Ceph (stg-nfs-02) | 10.40.1.241 | — | MON / MDS / RGW |

---

## SaaS Portal — `portal.aeisoftware.com`

The portal replaces `crear_instancia_odoo.sh` with a REST API + web dashboard.

| Endpoint | Description |
|:---|:---|
| `POST /api/instances` | Create Odoo instance (initContainers: DB restore + git addons) |
| `GET /api/instances` | List all instances with pod status |
| `DELETE /api/instances/{name}` | Delete instance + Cloudflare cleanup |
| `PATCH /api/instances/{name}/config` | Update `odoo.conf` per client |
| `POST /api/instances/{name}/restart` | Rolling restart |
| `GET /api/instances/{name}/logs` | Tail pod logs |
| `POST /api/templates/{path}` | Upload pg_dump to Ceph RGW S3 |
| `GET /api/templates` | List available DB templates |

**Auth:** `X-API-Key` header + Cloudflare Access MFA on the domain

---

## Quick Start — Deploy an Odoo Instance

### Via portal API
```bash
curl -X POST https://portal.aeisoftware.com/api/instances \
  -H "X-API-Key: <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "acme",
    "domain": "acme.aeisoftware.com",
    "odoo_version": "18",
    "db_template": "v18/starter.dump",
    "addons_repo": "https://github.com/your-org/odoo-addons.git",
    "odoo_conf_overrides": {"workers": 4}
  }'
```

### Via script (legacy)
```bash
export AZURE_PG_HOST="patroni-db.kube-system.svc.cluster.local"
export AZURE_PG_USER="odoo"
export AZURE_PG_PASSWORD="<password>"
export CF_API_TOKEN="<token>"
export CF_ACCOUNT_ID="<account>"
export CF_ZONE_ID="<zone>"
export CF_TUNNEL_ID="670c6e18-748b-4399-8fa4-7c78f3a1d342"

./crear_instancia_odoo.sh client1 client1.aeisoftware.com 18
kubectl apply -f k8s-client1/
```

### Remove an instance
```bash
# Via API (also cleans Cloudflare)
curl -X DELETE https://portal.aeisoftware.com/api/instances/acme \
  -H "X-API-Key: <key>" -G -d "domain=acme.aeisoftware.com"

# Via kubectl
kubectl delete ns odoo-<name>
```

---

## 1. Loading a Pre-configured Database Template

```bash
# Dump your configured Odoo database
pg_dump -h patroni-db.kube-system.svc.cluster.local -U odoo -Fc odoo_template > template.dump

# Upload to Ceph RGW via portal API
curl -X POST https://portal.aeisoftware.com/api/templates/v18/starter.dump \
  -H "X-API-Key: <key>" -F "file=@template.dump"
```

On instance create with `"db_template": "v18/starter.dump"`, an `initContainer` automatically restores it if the DB doesn't exist.

---

## 2. Custom Addons per Client

Set `addons_repo` when creating the instance. An `initContainer` runs `git clone/pull` on every pod start:

```json
{ "addons_repo": "https://github.com/your-org/client-addons.git" }
```

Update an existing client:
```bash
curl -X PATCH https://portal.aeisoftware.com/api/instances/client1/config \
  -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d '{"addons_repo": "https://github.com/your-org/new-addons.git"}'
```

---

## 3. Modifying odoo.conf

### For all future instances — edit the template in `crear_instancia_odoo.sh`
```bash
nano /home/ubuntu/aeisoftware/crear_instancia_odoo.sh
# Find: # --- 6. ConfigMap (odoo.conf) ---
```

### For a specific client
```bash
# Via API
curl -X PATCH https://portal.aeisoftware.com/api/instances/client1/config \
  -H "X-API-Key: <key>" -H "Content-Type: application/json" \
  -d '{"odoo_conf_overrides": {"workers": 4, "smtp_server": "smtp.client.com"}}'

# Via kubectl
kubectl edit configmap client1-odoo-conf -n odoo-client1
kubectl rollout restart deployment/client1-odoo -n odoo-client1
```

---

## 4. Custom Dockerfile

```dockerfile
FROM odoo:18
USER root
RUN pip3 install --no-cache-dir zeep paramiko pandas
COPY ./addons/my_module /usr/lib/python3/dist-packages/odoo/addons/my_module
USER odoo
```

```bash
# Build and import into K3s
docker build -t ghcr.io/jpvargassoruco/aeisoftware/odoo-custom:18 .
docker save ghcr.io/jpvargassoruco/aeisoftware/odoo-custom:18 | \
  ssh ubuntu@<worker> "sudo k3s ctr images import -"
```

GitHub Actions auto-builds on push to `portal/` or `docker/` — see `.github/workflows/`.

---

## Repository Structure

```
├── crear_instancia_odoo.sh          # Legacy script (still works)
├── portal/                          # SaaS REST API + Web Dashboard
│   ├── main.py                      # FastAPI app (API key auth)
│   ├── routers/instances.py         # Instance CRUD + Cloudflare
│   ├── routers/templates.py         # DB template upload/list (Ceph RGW)
│   ├── k8s_utils/manifests.py       # K8s manifest engine (replaces script)
│   ├── static/index.html            # Web dashboard
│   ├── Dockerfile
│   └── k8s-portal/portal.yaml      # RBAC + Deployment + Ingress
├── ceph/setup-ceph-csi.sh           # Ceph CSI driver setup
├── k3s-cluster/setup-k3s.sh
├── postgresql-ha/setup-patroni.sh
├── .github/workflows/
│   └── build-portal.yaml            # Auto-build portal image → ghcr.io
├── IMPLEMENTATION_PLAN.md
└── k8s-<client>/                    # Generated per-client manifests
    ├── 02-configmap.yaml            # ← odoo.conf
    ├── 03-pvc.yaml                  # ← ceph-cephfs data + addons
    └── 04-deployment.yaml           # ← initContainers + fsGroup:101
```

## Odoo Versions Supported

| Version | Image | Notes |
|:---|:---|:---|
| 17 | `odoo:17` | LTS |
| 18 | `odoo:18` | Current |
| 19 | `odoo:19` | Latest |

---

## GitHub Actions CI/CD

On push to `k3s-saas` branch (files in `portal/`):
1. Builds `portal/Dockerfile`
2. Pushes to `ghcr.io/jpvargassoruco/aeisoftware/saas-portal:latest`

**Requires:** Settings → Actions → General → Workflow permissions → "Read and write permissions"

---

## Roadmap

- [x] K3s HA cluster (3 CP + 3 workers, etcd)
- [x] Patroni PostgreSQL HA (3 nodes, HAProxy VIP)
- [x] Ceph distributed storage (RBD, CephFS, RGW)
- [x] Ceph CSI driver (ceph-rbd + ceph-cephfs StorageClasses)
- [x] SaaS Provisioning Portal (`portal.aeisoftware.com`)
- [x] DB template initContainer (pg_restore from Ceph RGW)
- [x] Git addons sync initContainer
- [x] GitHub Actions CI/CD for portal image
- [ ] Custom Odoo Dockerfile (per-version, baked addons)
- [ ] `odoo_k8s_saas` Odoo module (auto-provision on E-commerce sale)
- [ ] Prometheus + Grafana monitoring
- [ ] Rancher portal
