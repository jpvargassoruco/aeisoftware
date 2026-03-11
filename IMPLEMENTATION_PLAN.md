# Implementation Plan — Aeisoftware K3s SaaS Platform

## Completed

### Infrastructure Stack ✅
- K3s HA (3 control-planes etcd, 3 workers)
- Patroni PostgreSQL HA with **HAProxy VIP** (`patroni-db.kube-system:5432`) — failover is transparent
- Ceph (CephFS + RGW S3) with CSI driver, StorageClasses `ceph-rbd` and `ceph-cephfs`
- All Odoo PVCs on `ceph-cephfs` (RWX) — rolling updates work, no RBD lock deadlocks

### SaaS Provisioning Portal ✅
- FastAPI + Python `kubernetes` client — replaces `crear_instancia_odoo.sh`
- Deployed at `portal.aeisoftware.com` via Cloudflare Tunnel + Access MFA
- Web dashboard: create/delete/restart/logs/config per instance
- initContainer 1: pg_restore from Ceph RGW S3 template on first start
- initContainer 2: `git clone/pull` addons repo on every pod start
- GitHub Actions: auto-build portal image → `ghcr.io` on push

## Confirmed Decisions
- DB templates → **Ceph RGW S3** (`s3://odoo-templates/`, user `saas-portal`)
- Addons → **per-client git repo URL** (set at provision time)
- Auth → **API key** (`X-API-Key`) + **Cloudflare Access MFA**
- Registry → **ghcr.io**
- Domain → **portal.aeisoftware.com** via Cloudflare Tunnel

---

## Next: Custom Odoo Dockerfile

### Goal
Provide a `docker/Dockerfile.18` (and `.17`, `.19`) that:
- Pre-installs Python packages needed by common Odoo addons
- Can bake in company-wide modules
- Auto-builds via GitHub Actions → `ghcr.io/.../odoo-custom:18`
- Portal accepts `"image": "ghcr.io/.../odoo-custom:18"` in provision request

### Files
```
docker/
  Dockerfile.17
  Dockerfile.18
  Dockerfile.19
.github/workflows/
  build-odoo.yaml   ← build on push to docker/
```

---

## Next: odoo_k8s_saas Module

### Goal
Customer buys SaaS plan on Odoo E-commerce → Odoo auto-provisions their instance via the portal API.

### Architecture
```
sale.order confirm → odoo_k8s_saas → POST portal.aeisoftware.com/api/instances
                                            ↓
                                     K3s provisions Odoo instance
                                            ↓
                               Customer gets email with their URL
```

### Module Structure
```
odoo_k8s_saas/
├── __manifest__.py
├── models/
│   ├── saas_instance.py      # res.model tracking instances
│   └── sale_order.py         # hooks into sale.order confirm
├── views/
│   ├── saas_instance_views.xml
│   └── product_template_views.xml  # SaaS fields on products
├── data/
│   └── mail_template.xml     # "Your Odoo is ready" email
└── security/ir.model.access.csv
```

### Product configuration
Each Odoo product (SaaS plan) has:
- `saas_odoo_version` (17/18/19)
- `saas_db_template` (e.g. `v18/starter.dump`)
- `saas_addons_repo` (git URL)
- `saas_image` (optional custom image)
- `saas_domain_prefix` (auto or customer-chosen)

---

## Phase 7 — Remaining
- [ ] Prometheus + Grafana
- [ ] Rancher
