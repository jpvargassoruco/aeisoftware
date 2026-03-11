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
    ├── data  → ceph-cephfs (ReadWriteMany, CephFS)
    └── addons→ ceph-cephfs (ReadWriteMany, CephFS)
               │
    HAProxy VIP → patroni-db.kube-system:5432
               │
    Patroni PostgreSQL HA (3 nodes, auto-failover)
               │
        Ceph Storage Backend
        ├── CephFS → shared filesystem (data + addons)
        └── RGW   → S3 API (object storage)
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

## Quick Start — Deploy an Odoo Instance

```bash
export AZURE_PG_HOST="patroni-db.kube-system.svc.cluster.local"
export AZURE_PG_USER="odoo"
export AZURE_PG_PASSWORD="<password>"
export CF_API_TOKEN="<cloudflare-token>"
export CF_ACCOUNT_ID="<cf-account>"
export CF_ZONE_ID="<cf-zone>"
export CF_TUNNEL_ID="670c6e18-748b-4399-8fa4-7c78f3a1d342"

./crear_instancia_odoo.sh client1 client1.aeisoftware.com 18

kubectl apply -f k8s-client1/
```

### Remove an instance
```bash
kubectl delete ns odoo-<name>
```

---

## 1. Loading a Pre-configured Database Template

Use this to give every new client a pre-built Odoo database (modules installed, branding configured, demo data removed).

### Step 1 — Create the template on the Patroni leader
```bash
# Connect to Patroni leader
ssh ubuntu@10.40.2.193

# Create a clean Odoo instance, configure it, then dump it
pg_dump -U odoo -h localhost -Fc odoo_template > /tmp/odoo_template.dump

# Upload to Ceph RGW (S3)
aws s3 cp /tmp/odoo_template.dump s3://odoo-templates/v18/odoo_template.dump \
  --endpoint-url http://10.40.1.240:7480
```

### Step 2 — Add an initContainer to the Deployment
In `crear_instancia_odoo.sh`, add this `initContainer` before the main Odoo container:

```yaml
initContainers:
- name: restore-db
  image: postgres:16-alpine
  env:
  - name: PGPASSWORD
    valueFrom:
      secretKeyRef:
        name: ${K8S_NAME}-db-secret
        key: db_password
  command:
  - sh
  - -c
  - |
    # Only restore if the DB doesn't exist yet
    DB_EXISTS=$(psql -h patroni-db.kube-system.svc.cluster.local \
      -U odoo -tAc "SELECT 1 FROM pg_database WHERE datname='${K8S_NAME}'" 2>/dev/null)
    if [ "$DB_EXISTS" != "1" ]; then
      echo "Creating database from template..."
      createdb -h patroni-db.kube-system.svc.cluster.local -U odoo ${K8S_NAME}
      aws s3 cp s3://odoo-templates/v18/odoo_template.dump /tmp/template.dump \
        --endpoint-url http://10.40.1.240:7480
      pg_restore -h patroni-db.kube-system.svc.cluster.local \
        -U odoo -d ${K8S_NAME} /tmp/template.dump
      echo "Database restored from template."
    else
      echo "Database already exists, skipping restore."
    fi
```

### Alternative — PostgreSQL template databases (simpler, same server only)
```sql
-- On Patroni leader, make 'odoo_template' a true PG template:
UPDATE pg_database SET datistemplate=true WHERE datname='odoo_template';

-- Then in crear_instancia_odoo.sh, use createdb with -T flag:
-- createdb -T odoo_template <client_db>
```
> Note: PostgreSQL template databases only work when both DBs are on the same server.
> Since our Patroni VIP always routes to the leader, this works reliably.

---

## 2. Copying Custom Addons

All addons PVCs are `ceph-cephfs` (ReadWriteMany), so any worker can mount them.

### Option A — Copy directly into the running pod
```bash
# Copy a local addon folder into client1
kubectl cp ./my_module odoo-client1/$(kubectl get pod -n odoo-client1 -l app=odoo -o jsonpath='{.items[0].metadata.name}'):/mnt/extra-addons/

# Update the addon list and restart
kubectl exec -n odoo-client1 deploy/client1-odoo -- odoo --stop-after-init -u my_module
kubectl rollout restart deployment/client1-odoo -n odoo-client1
```

### Option B — Copy to all clients at once (script)
```bash
#!/bin/bash
MODULE_PATH="./my_module"
MODULE_NAME=$(basename $MODULE_PATH)

for client in client1 client2 client3; do
  POD=$(kubectl get pod -n odoo-${client} -l app=odoo -o jsonpath='{.items[0].metadata.name}')
  echo "Copying to $client ($POD)..."
  kubectl cp $MODULE_PATH odoo-${client}/${POD}:/mnt/extra-addons/
done
echo "Done. Restart pods to load the new addon."
```

### Option C — Use an initContainer with git (recommended for production)
Add to the Deployment in `crear_instancia_odoo.sh`:
```yaml
initContainers:
- name: sync-addons
  image: alpine/git
  command:
  - sh
  - -c
  - |
    git clone --depth=1 https://github.com/your-org/odoo-addons.git /mnt/extra-addons/
  volumeMounts:
  - name: odoo-addons
    mountPath: /mnt/extra-addons
```
Every pod start will pull the latest addons from your git repo automatically.

> **Tip:** Since the addons PVC is ReadWriteMany, all Odoo replicas (if you scale to >1) share the same addons volume automatically.

---

## 3. Modifying the odoo.conf Template (for all future clients)

The template lives in `crear_instancia_odoo.sh` around line 106. Edit the `02-configmap.yaml` heredoc:

```bash
nano /home/ubuntu/aeisoftware/crear_instancia_odoo.sh
# Look for: # --- 6. ConfigMap (odoo.conf) ---
```

Key parameters to customize:
```ini
[options]
workers = 4              # Increase for more concurrent users
max_cron_threads = 2     # Background job workers
limit_memory_hard = 4294967296  # 4GB RAM limit per worker
limit_time_real = 1800   # Request timeout (seconds)
log_level = warn         # debug | info | warn | error
```

After editing, new instances created with `./crear_instancia_odoo.sh` will use the updated template.

---

## 4. Modifying odoo.conf for a Specific Client

Each client's `odoo.conf` is stored as a Kubernetes ConfigMap:

```bash
# Edit interactively
kubectl edit configmap client1-odoo-conf -n odoo-client1

# Or patch a specific value (e.g., increase workers):
kubectl get configmap client1-odoo-conf -n odoo-client1 -o yaml | \
  sed 's/workers = 2/workers = 4/' | \
  kubectl apply -f -

# Restart the pod to apply changes (zero-downtime with CephFS RWX)
kubectl rollout restart deployment/client1-odoo -n odoo-client1
kubectl rollout status deployment/client1-odoo -n odoo-client1
```

Common per-client customizations:
```ini
[options]
# Client-specific DB (already set by the script)
db_name = client1
db_filter = client1

# Custom SMTP
smtp_server = smtp.client1.com
smtp_port = 587
smtp_user = odoo@client1.com
smtp_password = secret

# Different currency
```

---

## 5. Custom Dockerfile (Modify the Odoo Image)

Use a Dockerfile to: pre-install Python packages, bake in addons, change locale, add fonts, etc.

### Example Dockerfile
```dockerfile
FROM odoo:18

USER root

# Install extra Python packages
RUN pip3 install --no-cache-dir \
    zeep \
    paramiko \
    pandas

# Pre-bake custom addons into the image (alternative to CephFS copy)
COPY ./addons/my_module /usr/lib/python3/dist-packages/odoo/addons/my_module

# Install system dependencies (e.g., wkhtmltopdf for PDF reports)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wkhtmltopdf \
    && rm -rf /var/lib/apt/lists/*

USER odoo
```

### Build and push
```bash
# Build for your cluster's architecture (linux/amd64)
docker build -t ghcr.io/your-org/odoo-custom:18 .
docker push ghcr.io/your-org/odoo-custom:18
```

### Use in crear_instancia_odoo.sh
Find the image line and change it:
```bash
# In crear_instancia_odoo.sh, around line 208:
# Find:  image: odoo:${ODOO_VERSION}
# Change to use a custom image map:
```

Or set it per-client after deploy:
```bash
kubectl set image deployment/client1-odoo \
  odoo=ghcr.io/your-org/odoo-custom:18 \
  -n odoo-client1
```

### GitHub Actions — auto-build on push
```yaml
# .github/workflows/build-odoo.yaml
name: Build Custom Odoo
on:
  push:
    paths: ['Dockerfile', 'addons/**']
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
    - uses: docker/login-action@v3
      with:
        registry: ghcr.io
        username: ${{ github.actor }}
        password: ${{ secrets.GITHUB_TOKEN }}
    - uses: docker/build-push-action@v5
      with:
        push: true
        tags: ghcr.io/${{ github.repository }}/odoo-custom:18
```

---

## Repository Structure

```
├── crear_instancia_odoo.sh      # Main provisioning script
├── cloudflare_provision.py      # Cloudflare DNS/Tunnel automation
├── k3s-cluster/setup-k3s.sh    # K3s HA cluster setup
├── postgresql-ha/setup-patroni.sh
├── ceph/setup-ceph-csi.sh       # Ceph CSI driver setup
├── Dockerfile                   # (optional) Custom Odoo image
├── addons/                      # (optional) Custom addons to bake in
├── IMPLEMENTATION_PLAN.md
└── k8s-<client>/                # Generated per-client manifests
    ├── 01-namespace.yaml
    ├── 01-secret.yaml
    ├── 02-configmap.yaml        # ← odoo.conf lives here
    ├── 03-pvc.yaml              # ← ceph-cephfs data + addons
    ├── 04-deployment.yaml
    ├── 05-service.yaml
    └── 06-ingress.yaml
```

## Odoo Versions Supported

| Version | Image |
|:---|:---|
| 17 | `odoo:17` |
| 18 | `odoo:18` |
| 19 | `odoo:19` |

---

## Roadmap

- [ ] DB template initContainer in crear_instancia_odoo.sh
- [ ] Addon sync via git initContainer
- [ ] Custom Dockerfile + GitHub Actions CI/CD
- [ ] Rancher portal
- [ ] Prometheus + Grafana
- [ ] `odoo_k8s_saas` module — auto-provision from E-commerce
- [ ] Azure migration
