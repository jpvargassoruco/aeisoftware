import os
from kubernetes import client, config

def get_k8s_client():
    """Load in-cluster config when running in K3s, fallback to kubeconfig locally."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api(), client.NetworkingV1Api()


# ─── Environment ────────────────────────────────────────────
PATRONI_HOST    = os.getenv("PATRONI_HOST", "patroni-db.kube-system.svc.cluster.local")
PATRONI_PORT    = os.getenv("PATRONI_PORT", "5432")
PATRONI_USER    = os.getenv("PATRONI_USER", "odoo")
PATRONI_PASS    = os.getenv("PATRONI_PASS", "")
CF_API_TOKEN    = os.getenv("CF_API_TOKEN", "")
CF_ACCOUNT_ID   = os.getenv("CF_ACCOUNT_ID", "")
CF_ZONE_ID      = os.getenv("CF_ZONE_ID", "")
CF_TUNNEL_ID    = os.getenv("CF_TUNNEL_ID", "")
S3_ENDPOINT     = os.getenv("S3_ENDPOINT", "http://10.40.1.240:7480")
S3_ACCESS_KEY   = os.getenv("S3_ACCESS_KEY", "aeisoftware")
S3_SECRET_KEY   = os.getenv("S3_SECRET_KEY", "")
S3_BUCKET       = os.getenv("S3_BUCKET", "odoo-templates")
SUPPORTED_VERSIONS = ["17", "18", "19"]


def build_namespace(name: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": f"odoo-{name}", "labels": {"managed-by": "saas-portal"}},
    }


def build_secret(name: str, db_pass: str) -> dict:
    import base64
    def b64(s): return base64.b64encode(s.encode()).decode()
    return {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": f"{name}-db-secret", "namespace": f"odoo-{name}"},
        "type": "Opaque",
        "data": {
            # Isolation is achieved via per-instance DATABASE + db_filter in odoo.conf.
            "db_host":     b64(PATRONI_HOST),
            "db_port":     b64(PATRONI_PORT),
            "db_user":     b64(PATRONI_USER),   # admin odoo user, allowed by pg_hba
            "db_password": b64(PATRONI_PASS),   # admin odoo password
            "admin_passwd": b64(db_pass),        # Odoo master password (admin_passwd)
        },
    }


def build_configmap(name: str, domain: str, db_pass: str, overrides: dict,
                    addons_repos=None) -> dict:
    """
    Build odoo.conf ConfigMap.
    db_filter = {name} allows clients to see all databases containing
    the instance name (e.g. test19, test19-backup, test19-sales).
    No db_name set — Odoo uses db_filter to discover databases.
    admin_passwd is set from db_pass so the database manager has a
    fixed, known master password (prevents Odoo from generating random ones).
    """
    defaults = {
        "workers": 2, "max_cron_threads": 1, "gevent_port": 8072,
        "limit_memory_hard": 2684354560, "limit_memory_soft": 2147483648,
        "limit_request": 8192, "limit_time_cpu": 600, "limit_time_real": 1200,
    }
    cfg = {**defaults, **overrides}
    # Build addons_path: base path + one entry per cloned repo subdirectory
    addons_paths = ["/mnt/extra-addons"]
    if addons_repos:
        for repo in addons_repos:
            url = repo.url if hasattr(repo, 'url') else repo.get('url', '')
            repo_name = url.rstrip('/').split('/')[-1].removesuffix('.git')
            addons_paths.append(f"/mnt/extra-addons/{repo_name}")
    addons_paths.append("/usr/lib/python3/dist-packages/odoo/addons")
    addons_path_str = ",".join(addons_paths)
    conf = f"""[options]
db_host = {PATRONI_HOST}
db_port = {PATRONI_PORT}
db_user = {PATRONI_USER}
db_password = {PATRONI_PASS}
admin_passwd = {db_pass}
dbfilter = {name}
list_db = True
addons_path = {addons_path_str}
data_dir = /var/lib/odoo
workers = {cfg['workers']}
max_cron_threads = {cfg['max_cron_threads']}
gevent_port = {cfg['gevent_port']}
proxy_mode = True
limit_memory_hard = {cfg['limit_memory_hard']}
limit_memory_soft = {cfg['limit_memory_soft']}
limit_request = {cfg['limit_request']}
limit_time_cpu = {cfg['limit_time_cpu']}
limit_time_real = {cfg['limit_time_real']}
"""
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": f"{name}-odoo-conf", "namespace": f"odoo-{name}"},
        "data": {"odoo.conf": conf},
    }




def build_pvcs(name: str) -> list:
    def pvc(pvc_name, size, desc):
        return {
            "apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": pvc_name, "namespace": f"odoo-{name}",
                "annotations": {"description": desc},
            },
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "storageClassName": "ceph-cephfs",
                "resources": {"requests": {"storage": size}},
            },
        }
    return [
        pvc(f"{name}-odoo-data",   "10Gi", "Odoo filestore — CephFS RWX"),
        pvc(f"{name}-odoo-addons", "5Gi",  "Custom addons — CephFS RWX"),
    ]


def build_deployment(
    name: str,
    version: str,
    image: str | None,
    db_template: str | None,
    addons_repos: list,          # list of AddonRepo objects or dicts {url, branch}
) -> dict:
    import json
    odoo_image = image or f"odoo:{version}"
    init_containers = []

    # ── initContainer: setup-db ──────────────────────────────────────────────────
    # Only verifies Patroni connectivity. DB creation and initialization are
    # handled by the portal background task via Odoo's native API:
    #   • Fresh install  → POST /web/database/create
    #   • ZIP restore    → POST /web/database/restore
    # Odoo starts in "no database" (nodb) mode — /web/health returns 200
    # until the database is created by the background task.
    db_setup_script = f"""#!/bin/sh
set -e
echo "[init] Verifying Patroni connectivity..."
PGPASSWORD={PATRONI_PASS} psql -h {PATRONI_HOST} -p {PATRONI_PORT} -U {PATRONI_USER} \\
  -d postgres -tAc "SELECT 1" > /dev/null && echo "[init] Patroni OK." || \\
  {{ echo "[init] ERROR: Cannot reach Patroni at {PATRONI_HOST}:{PATRONI_PORT}"; exit 1; }}
echo "[init] Setup complete — DB will be created by the portal via Odoo API."
"""

    init_containers.append({
        "name": "setup-db",
        "image": "postgres:17-alpine",
        "command": ["sh", "-c", db_setup_script],
        "env": [
            {"name": "AWS_ACCESS_KEY_ID",     "value": S3_ACCESS_KEY},
            {"name": "AWS_SECRET_ACCESS_KEY", "value": S3_SECRET_KEY},
            {"name": "AWS_DEFAULT_REGION",    "value": "us-east-1"},
        ],
    })


    # initContainer 2: sync addons — one directory per repo
    # Each repo clones into /mnt/extra-addons/<repo-name>/
    if addons_repos:
        clone_cmds = []
        for repo in addons_repos:
            # Support both AddonRepo objects and plain dicts
            url = repo.url if hasattr(repo, 'url') else repo['url']
            branch = (repo.branch if hasattr(repo, 'branch') else repo.get('branch')) or ""
            # Derive a short directory name from the repo URL
            repo_name = url.rstrip("/").split("/")[-1].removesuffix(".git")
            dest = f"/mnt/extra-addons/{repo_name}"
            branch_flag = f"--branch {branch} " if branch else ""
            clone_cmds.append(f"""
if [ -d {dest}/.git ]; then
  echo "[init] Pulling {repo_name}..."
  cd {dest} && git pull --ff-only 2>&1 || git fetch --all && git reset --hard origin/HEAD
else
  echo "[init] Cloning {repo_name} ({url})..."
  git clone --depth=1 {branch_flag}{url} {dest}
fi
echo "[init] {repo_name} ready."
""".strip())

        init_containers.append({
            "name": "sync-addons",
            "image": "alpine/git:latest",
            "command": ["sh", "-c", "\n".join(clone_cmds)],
            "volumeMounts": [{"name": "odoo-addons", "mountPath": "/mnt/extra-addons"}],
        })

    # Build repos annotation for display in the portal
    repos_annotation = json.dumps([
        {"url": (r.url if hasattr(r, 'url') else r['url']),
         "branch": (r.branch if hasattr(r, 'branch') else r.get('branch'))}
        for r in addons_repos
    ]) if addons_repos else ""

    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {
            "name": f"{name}-odoo", "namespace": f"odoo-{name}",
            "labels": {"app": "odoo", "client": name, "odoo-version": version},
            "annotations": {
                "saas/addons-repos": repos_annotation,
                "saas/db-template": db_template or "",
                "saas/image": odoo_image,
                "saas/protected": "false",
            },
        },
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": "odoo", "client": name}},
            "template": {
                "metadata": {"labels": {"app": "odoo", "client": name, "odoo-version": version}},
                "spec": {
                    "securityContext": {"fsGroup": 101},
                    "initContainers": init_containers,
                    "containers": [{
                        "name": "odoo",
                        "image": odoo_image,
                        "ports": [
                            {"containerPort": 8069, "name": "http"},
                            {"containerPort": 8072, "name": "longpoll"},
                        ],
                        "envFrom": [{"secretRef": {"name": f"{name}-db-secret"}}],
                        "volumeMounts": [
                            {"name": "odoo-conf",   "mountPath": "/etc/odoo"},
                            {"name": "odoo-data",   "mountPath": "/var/lib/odoo"},
                            {"name": "odoo-addons", "mountPath": "/mnt/extra-addons"},
                        ],
                        "readinessProbe": {
                            "httpGet": {"path": "/web/health", "port": 8069},
                            "initialDelaySeconds": 30, "periodSeconds": 10, "failureThreshold": 6,
                        },
                        "resources": {
                            "requests": {"cpu": "200m", "memory": "512Mi"},
                            "limits":   {"cpu": "2",    "memory": "2Gi"},
                        },
                    }],
                    "volumes": [
                        {"name": "odoo-conf",   "configMap": {"name": f"{name}-odoo-conf"}},
                        {"name": "odoo-data",   "persistentVolumeClaim": {"claimName": f"{name}-odoo-data"}},
                        {"name": "odoo-addons", "persistentVolumeClaim": {"claimName": f"{name}-odoo-addons"}},
                    ],
                },
            },
        },
    }



def build_service(name: str) -> dict:
    return {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": f"{name}-odoo-svc", "namespace": f"odoo-{name}"},
        "spec": {
            "selector": {"app": "odoo", "client": name},
            "ports": [
                {"name": "http",     "port": 8069, "targetPort": 8069},
                {"name": "longpoll", "port": 8072, "targetPort": 8072},
            ],
        },
    }


def build_ingress(name: str, domain: str) -> dict:
    """Build Ingress: / → 8069, /websocket → 8072, with X-Forwarded-Proto middleware.

    Requires the 'odoo-headers' Traefik Middleware to exist in kube-system:
      kubectl apply -f k8s-client1/odoo-headers-middleware.yaml
    """
    return {
        "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
        "metadata": {
            "name": f"{name}-odoo-ingress", "namespace": f"odoo-{name}",
            "annotations": {
                "traefik.ingress.kubernetes.io/router.entrypoints": "web",
                # References the Middleware CRD: <namespace>-<name>@kubernetescrd
                "traefik.ingress.kubernetes.io/router.middlewares": "kube-system-odoo-headers@kubernetescrd",
            },
        },
        "spec": {
            "ingressClassName": "traefik",
            "rules": [{
                "host": domain,
                "http": {"paths": [
                    # WebSocket/longpoll must come first (more specific)
                    {"path": "/websocket", "pathType": "Prefix",
                     "backend": {"service": {"name": f"{name}-odoo-svc", "port": {"number": 8072}}}},
                    # Main Odoo HTTP
                    {"path": "/", "pathType": "Prefix",
                     "backend": {"service": {"name": f"{name}-odoo-svc", "port": {"number": 8069}}}},
                ]},
            }],
        },
    }
