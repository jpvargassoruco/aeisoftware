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
            "db_host":     b64(PATRONI_HOST),
            "db_port":     b64(PATRONI_PORT),
            "db_user":     b64(PATRONI_USER),
            "db_password": b64(db_pass),
        },
    }


def build_configmap(name: str, domain: str, db_pass: str, overrides: dict) -> dict:
    defaults = {
        # workers=0 = threaded mode — avoids HAProxy idle-connection drops on long-lived workers
        "workers": 0, "max_cron_threads": 1, "gevent_port": 8072,
        "limit_memory_hard": 2684354560, "limit_memory_soft": 2147483648,
        "limit_request": 8192, "limit_time_cpu": 600, "limit_time_real": 1200,
    }
    cfg = {**defaults, **overrides}
    conf = f"""[options]
db_host = {PATRONI_HOST}
db_port = {PATRONI_PORT}
db_user = {PATRONI_USER}
db_password = {db_pass}
db_name = {name}
db_filter = {name}
admin_passwd = {overrides.get('admin_passwd', 'admin')}
addons_path = /mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons
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
    addons_repo: str | None,
) -> dict:
    odoo_image = image or f"odoo:{version}"
    init_containers = []

    # initContainer 1: restore DB from Ceph RGW template
    if db_template:
        init_containers.append({
            "name": "restore-db",
            "image": "postgres:16-alpine",
            "command": ["sh", "-c", f"""
apk add --no-cache aws-cli 2>/dev/null || true
DB_EXISTS=$(PGPASSWORD=$DB_PASS psql -h {PATRONI_HOST} -U {PATRONI_USER} \
  -tAc "SELECT 1 FROM pg_database WHERE datname='{name}'" 2>/dev/null || echo "")
if [ "$DB_EXISTS" != "1" ]; then
  echo "[init] Creating database {name} from template {db_template}..."
  PGPASSWORD=$DB_PASS createdb -h {PATRONI_HOST} -U {PATRONI_USER} {name}
  aws s3 cp s3://{S3_BUCKET}/{db_template} /tmp/template.dump \
    --endpoint-url {S3_ENDPOINT} \
    --no-verify-ssl 2>&1
  PGPASSWORD=$DB_PASS pg_restore -h {PATRONI_HOST} -U {PATRONI_USER} \
    -d {name} --no-owner --role={PATRONI_USER} /tmp/template.dump || true
  echo "[init] Database restored."
else
  echo "[init] Database {name} already exists, skipping."
fi
""".strip()],
            "env": [
                {"name": "DB_PASS", "valueFrom": {"secretKeyRef": {"name": f"{name}-db-secret", "key": "db_password"}}},
                {"name": "AWS_ACCESS_KEY_ID",     "value": S3_ACCESS_KEY},
                {"name": "AWS_SECRET_ACCESS_KEY", "value": S3_SECRET_KEY},
                {"name": "AWS_DEFAULT_REGION",    "value": "us-east-1"},
            ],
        })

    # initContainer 2: sync addons from git repo
    if addons_repo:
        init_containers.append({
            "name": "sync-addons",
            "image": "alpine/git:latest",
            "command": ["sh", "-c", f"""
if [ -d /mnt/extra-addons/.git ]; then
  echo "[init] Pulling latest addons..."
  cd /mnt/extra-addons && git pull --ff-only
else
  echo "[init] Cloning addons from {addons_repo}..."
  git clone --depth=1 {addons_repo} /tmp/addons_clone
  cp -r /tmp/addons_clone/. /mnt/extra-addons/
fi
echo "[init] Addons ready."
""".strip()],
            "volumeMounts": [{"name": "odoo-addons", "mountPath": "/mnt/extra-addons"}],
        })

    return {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {
            "name": f"{name}-odoo", "namespace": f"odoo-{name}",
            "labels": {"app": "odoo", "client": name, "odoo-version": version},
            "annotations": {
                "saas/addons-repo": addons_repo or "",
                "saas/db-template": db_template or "",
                "saas/image": odoo_image,
            },
        },
        "spec": {
            "replicas": 1,
            "strategy": {"type": "RollingUpdate"},
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
                            {"name": "odoo-conf", "mountPath": "/etc/odoo"},
                            {"name": "odoo-data", "mountPath": "/var/lib/odoo"},
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
    """Build Ingress matching crear_instancia_odoo.sh: 2 paths (8069 + /websocket→8072)."""
    return {
        "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
        "metadata": {
            "name": f"{name}-odoo-ingress", "namespace": f"odoo-{name}",
            "annotations": {
                "traefik.ingress.kubernetes.io/router.entrypoints": "web",
                "traefik.ingress.kubernetes.io/custom-request-headers": "X-Forwarded-Proto: https",
            },
        },
        "spec": {
            "rules": [{
                "host": domain,
                "http": {"paths": [
                    # Websocket/longpoll (Odoo live chat, discuss)
                    {"path": "/websocket", "pathType": "Prefix",
                     "backend": {"service": {"name": f"{name}-odoo-svc", "port": {"number": 8072}}}},
                    # Main HTTP
                    {"path": "/", "pathType": "Prefix",
                     "backend": {"service": {"name": f"{name}-odoo-svc", "port": {"number": 8069}}}},
                ]},
            }],
        },
    }
