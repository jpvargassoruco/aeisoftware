from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel, Field
from typing import Optional, List
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import httpx, os, re, asyncio, io
import boto3
from botocore.client import Config
from k8s_utils.manifests import (
    build_namespace, build_secret, build_configmap,
    build_pvcs, build_deployment, build_service, build_ingress,
    SUPPORTED_VERSIONS, CF_API_TOKEN, CF_ACCOUNT_ID, CF_ZONE_ID, CF_TUNNEL_ID,
    S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET,
)

router = APIRouter()


# ─── Models ────────────────────────────────────────────────────────────────────

class AddonRepo(BaseModel):
    url: str                          # e.g. "https://github.com/org/addons.git"
    branch: Optional[str] = None     # e.g. "17.0", defaults to repo default


class InstanceCreate(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9][a-z0-9\-]{1,30}$")
    domain: str
    odoo_version: str = Field("18", pattern=r"^(17|18|19)$")
    db_template: Optional[str] = None          # e.g. "v18/backup.zip" — ZIP only
    addons_repos: List[AddonRepo] = Field(default_factory=list)
    image: Optional[str] = None                # override image
    db_password: str = Field(default="odoo")   # PostgreSQL connection password
    admin_passwd: str = Field(...)             # REQUIRED: Odoo master/manager password
    admin_email: str = Field(...)             # REQUIRED: admin user login email
    admin_password: str = Field(...)          # REQUIRED: admin user initial password
    lang: str = Field(default="en_US")        # DB language for fresh installs
    odoo_conf_overrides: dict = Field(default_factory=dict)


class InstancePatch(BaseModel):
    odoo_conf_overrides: Optional[dict] = None
    addons_repos: Optional[List[AddonRepo]] = None


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _k8s():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api(), client.NetworkingV1Api()


async def _drop_pg_resources(name: str):
    """Drop ALL PostgreSQL databases belonging to this instance on deletion.

    The dbfilter for instance 'saas' is the regex 'saas', which matches
    databases named 'saas', 'saas2', 'saas3', etc. — all created via the
    Odoo database manager. We drop every database whose name starts with
    the instance name to avoid leaving garbage in Patroni.
    Also drops the per-instance Postgres role if it exists.
    """
    import psycopg2
    from psycopg2 import sql
    from k8s_utils.manifests import PATRONI_HOST, PATRONI_PORT, PATRONI_USER, PATRONI_PASS
    try:
        conn = psycopg2.connect(
            host=PATRONI_HOST, port=int(PATRONI_PORT),
            user=PATRONI_USER, password=PATRONI_PASS,
            dbname="postgres", connect_timeout=10
        )
        conn.autocommit = True
        cur = conn.cursor()

        # Find all databases whose name starts with the instance slug
        cur.execute(
            "SELECT datname FROM pg_database "
            "WHERE datistemplate = false AND datname LIKE %s",
            (name + "%",)
        )
        databases = [row[0] for row in cur.fetchall()]
        print(f"[portal] Databases to drop for instance '{name}': {databases}")

        for dbname in databases:
            # Terminate active connections before dropping
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (dbname,)
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)))
            print(f"[portal] Dropped database: {dbname}")

        # Drop the per-instance role (idempotent)
        cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        conn.close()
        print(f"[portal] Cleaned up all PostgreSQL resources for instance: {name}")
    except Exception as e:
        # Log but don't fail the delete — K8s namespace is already being removed
        print(f"[portal] Warning: could not drop PostgreSQL resources for {name}: {e}")



def _safe_create(fn, *args, **kwargs):
    """Create K8s resource, ignore 409 AlreadyExists."""
    try:
        fn(*args, **kwargs)
    except ApiException as e:
        if e.status != 409:
            raise HTTPException(status_code=500, detail=str(e))


def _repos_annotation(repos: List[AddonRepo]) -> str:
    import json
    return json.dumps([{"url": r.url, "branch": r.branch} for r in repos])


async def _configure_cloudflare(name: str, domain: str):
    """Add Cloudflare DNS CNAME + Tunnel ingress route (idempotent)."""
    if not all([CF_API_TOKEN, CF_ZONE_ID, CF_TUNNEL_ID, CF_ACCOUNT_ID]):
        print("[cf] Skipping: CF credentials not set")
        return
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}
    tunnel_domain = f"{CF_TUNNEL_ID}.cfargotunnel.com"
    # Use explicit timeout for each phase (connect is often the slow one inside K8s)
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        # 1. Add/update DNS record (upsert: delete existing first, then create)
        existing = await http.get(
            f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records?name={domain}",
            headers=headers,
        )
        for rec in existing.json().get("result", []):
            await http.delete(
                f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/{rec['id']}",
                headers=headers,
            )
        dns_r = await http.post(
            f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records",
            headers=headers,
            json={"type": "CNAME", "name": domain, "content": tunnel_domain,
                  "proxied": True, "ttl": 1},
        )
        print(f"[cf] DNS upsert {domain}: {dns_r.status_code}")

        # 2. Update tunnel ingress (remove existing entry for this domain, then prepend)
        r = await http.get(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations",
            headers=headers,
        )
        current = r.json().get("result", {}).get("config", {})
        ingress = current.get("ingress", [{"service": "http_status:404"}])
        # Remove any existing entry for this domain (prevent duplicates)
        ingress_filtered = [i for i in ingress if i.get("hostname") != domain]
        ingress_no_catch = [i for i in ingress_filtered if i.get("hostname")]
        catch_all       = [i for i in ingress_filtered if not i.get("hostname")]
        new_route = {"hostname": domain, "service": "http://traefik.kube-system.svc.cluster.local:80"}
        tunnel_r = await http.put(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations",
            headers=headers,
            json={"config": {"ingress": ingress_no_catch + [new_route] + catch_all}},
        )
        print(f"[cf] Tunnel ingress add {domain}: {tunnel_r.status_code}")


async def _remove_cloudflare(domain: str):
    """Remove Cloudflare DNS CNAME + Tunnel ingress route for this domain."""
    if not all([CF_API_TOKEN, CF_ZONE_ID, CF_TUNNEL_ID, CF_ACCOUNT_ID]):
        print("[cf] Skipping remove: CF credentials not set")
        return
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        # 1. Delete DNS CNAME record(s) for this domain
        r = await http.get(
            f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records?name={domain}",
            headers=headers,
        )
        dns_records = r.json().get("result", [])
        print(f"[cf] Found {len(dns_records)} DNS record(s) for {domain}")
        for record in dns_records:
            del_r = await http.delete(
                f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/{record['id']}",
                headers=headers,
            )
            print(f"[cf] DNS delete {record['id']}: {del_r.status_code}")

        # 2. Remove from tunnel ingress configuration
        r2 = await http.get(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations",
            headers=headers,
        )
        current = r2.json().get("result", {}).get("config", {})
        all_ingress = current.get("ingress", [])
        filtered   = [i for i in all_ingress if i.get("hostname") != domain]
        removed    = len(all_ingress) - len(filtered)
        print(f"[cf] Removing {removed} tunnel route(s) for {domain} (was {len(all_ingress)}, now {len(filtered)})")
        tunnel_r = await http.put(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations",
            headers=headers,
            json={"config": {"ingress": filtered}},
        )
        print(f"[cf] Tunnel ingress update: {tunnel_r.status_code} — {tunnel_r.text[:120]}")


# ─── Create ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_instance(body: InstanceCreate):
    from fastapi import HTTPException
    core, apps, net = _k8s()
    ns = f"odoo-{body.name}"

    # ── Pre-flight: duplicate name check ────────────────────────────────────────
    existing_ns = core.list_namespace(label_selector="managed-by=saas-portal")
    existing_names = [n.metadata.name.removeprefix("odoo-") for n in existing_ns.items]
    if body.name in existing_names:
        raise HTTPException(
            status_code=409,
            detail=f"Instance '{body.name}' already exists. Please choose a different name."
        )

    # ── Pre-flight: duplicate domain check ──────────────────────────────────────
    for existing_name in existing_names:
        try:
            ingress = net.read_namespaced_ingress(
                f"{existing_name}-odoo-ingress", f"odoo-{existing_name}"
            )
            for rule in (ingress.spec.rules or []):
                if rule.host == body.domain:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Domain '{body.domain}' is already in use by instance '{existing_name}'. Please choose a different domain."
                    )
        except HTTPException:
            raise
        except Exception:
            pass  # ignore read errors for individual instances

    _safe_create(core.create_namespace, build_namespace(body.name))
    _safe_create(core.create_namespaced_secret, ns,
                 build_secret(body.name, body.admin_passwd))
    _safe_create(core.create_namespaced_config_map, ns,
                 build_configmap(body.name, body.domain, body.admin_passwd,
                                body.odoo_conf_overrides, body.addons_repos))

    for pvc in build_pvcs(body.name):
        _safe_create(core.create_namespaced_persistent_volume_claim, ns, pvc)

    deployment = build_deployment(
        body.name, body.odoo_version, body.image,
        body.db_template, body.addons_repos)
    _safe_create(apps.create_namespaced_deployment, ns, deployment)
    _safe_create(core.create_namespaced_service, ns, build_service(body.name))
    _safe_create(net.create_namespaced_ingress, ns, build_ingress(body.name, body.domain))

    cf_warning = None
    try:
        await _configure_cloudflare(body.name, body.domain)
    except Exception as cf_err:
        cf_warning = str(cf_err)
        print(f"[cf] WARNING: could not configure Cloudflare for {body.domain}: {cf_err}")

    # ── Spawn background task to initialize the database via Odoo native API ──
    if body.db_template:
        # ZIP restore: /web/database/restore handles DB creation + data + filestore
        asyncio.create_task(_restore_via_odoo(
            name=body.name,
            domain=body.domain,
            db_template=body.db_template,
            admin_passwd=body.admin_passwd,
        ))
    else:
        # Fresh install: /web/database/create initializes Odoo with admin user
        asyncio.create_task(_initialize_fresh_db(
            name=body.name,
            domain=body.domain,
            admin_passwd=body.admin_passwd,
            admin_email=body.admin_email,
            admin_password=body.admin_password,
            lang=body.lang,
        ))

    return {"name": body.name, "domain": body.domain,
            "url": f"https://{body.domain}", "status": "provisioning",
            "cf_warning": cf_warning}


def _restart_odoo_pod(name: str):
    """Restart the Odoo deployment so it cleanly initializes modules from the new/restored DB.

    After /web/database/create or /web/database/restore, Odoo holds a partially-initialized
    registry that causes KeyError: 'ir.http' on /web/health. A pod restart forces Odoo to
    discover the database via dbfilter and load all modules cleanly.

    Also patches the ConfigMap to set list_db = False, disabling the database manager UI
    for clients. During initial provisioning, list_db = True is required so the portal's
    background task can call /web/database/create or /web/database/restore.
    """
    import datetime
    core, apps, _ = _k8s()
    ns = f"odoo-{name}"

    # Disable database manager after provisioning
    try:
        cm = core.read_namespaced_config_map(f"{name}-odoo-conf", ns)
        conf = cm.data.get("odoo.conf", "")
        if "list_db = True" in conf:
            cm.data["odoo.conf"] = conf.replace("list_db = True", "list_db = False")
            core.patch_namespaced_config_map(f"{name}-odoo-conf", ns, {"data": cm.data})
            print(f"[portal] Patched {name} ConfigMap: list_db = False")
    except Exception as e:
        print(f"[portal] WARNING: Could not patch ConfigMap for {name}: {e}")

    # Restart deployment and mark as ready
    patch = {"spec": {"template": {"metadata": {"annotations":
        {"kubectl.kubernetes.io/restartedAt": datetime.datetime.utcnow().isoformat()}}}},
        "metadata": {"annotations": {"saas/status": "ready"}}}
    try:
        apps.patch_namespaced_deployment(f"{name}-odoo", ns, patch)
        print(f"[portal] Restarted {name}-odoo deployment — status: ready")
    except Exception as e:
        print(f"[portal] WARNING: Could not restart {name}-odoo: {e}")



async def _initialize_fresh_db(
    name: str, domain: str, admin_passwd: str,
    admin_email: str, admin_password: str, lang: str = "en_US"
):
    """Background task: initialize a fresh Odoo database via /web/database/create.

    Odoo starts in nodb mode (no matching DB). Once healthy, we POST to
    /web/database/create with the user's credentials. Odoo creates the database,
    installs base modules, creates the admin user, and starts serving.
    Finally we fix web.base.url to the correct domain.
    """
    import psycopg2
    from k8s_utils.manifests import PATRONI_HOST, PATRONI_PORT, PATRONI_USER, PATRONI_PASS

    internal_url = f"http://{name}-odoo-svc.odoo-{name}.svc.cluster.local:8069"
    print(f"[db-create] Starting fresh DB initialization for {name}")

    # Wait for Odoo to be reachable in nodb mode
    await asyncio.sleep(20)
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=False) as http:
        for attempt in range(40):
            try:
                r = await http.get(f"{internal_url}/web/health")
                if r.status_code < 500:
                    print(f"[db-create] Odoo {name} ready (HTTP {r.status_code}) after {attempt*15+20}s")
                    break
                print(f"[db-create] Health {attempt+1}/40: HTTP {r.status_code}")
            except Exception as e:
                print(f"[db-create] Health {attempt+1}/40: {type(e).__name__}")
            await asyncio.sleep(15)
        else:
            print(f"[db-create] ERROR: Timeout waiting for Odoo {name}")
            return

    # Call /web/database/create — Odoo initializes the DB with admin user
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=5.0)) as http:
            r = await http.post(
                f"{internal_url}/web/database/create",
                data={
                    "master_pwd": admin_passwd,
                    "name": name,
                    "login": admin_email,
                    "password": admin_password,
                    "lang": lang,
                    "demo": "false",
                },
            )
            print(f"[db-create] Odoo create response: HTTP {r.status_code}")
    except Exception as e:
        print(f"[db-create] ERROR: DB create request failed: {e}")
        return

    # Verify the database was actually created (Odoo returns 200 even on failure)
    await asyncio.sleep(5)
    try:
        conn = psycopg2.connect(
            host=PATRONI_HOST, port=int(PATRONI_PORT),
            user=PATRONI_USER, password=PATRONI_PASS, dbname='postgres'
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
        if not cur.fetchone():
            print(f"[db-create] ERROR: Odoo returned HTTP 200 but database '{name}' was NOT created!")
            print(f"[db-create] This usually means the master password (admin_passwd) is wrong.")
            print(f"[db-create] Check that admin_passwd matches odoo.conf's admin_passwd setting.")
            conn.close()
            return
        print(f"[db-create] Verified: database '{name}' exists in PostgreSQL")

        # Fix web.base.url
        conn.close()
        conn = psycopg2.connect(
            host=PATRONI_HOST, port=int(PATRONI_PORT),
            user=PATRONI_USER, password=PATRONI_PASS, dbname=name
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "UPDATE ir_config_parameter SET value=%s WHERE key IN (%s,%s)",
            (f"https://{domain}", 'web.base.url', 'web.base.url.static')
        )
        print(f"[db-create] Updated web.base.url to https://{domain} ({cur.rowcount} rows)")
        conn.close()
    except Exception as e:
        print(f"[db-create] ERROR verifying/configuring DB: {e}")
        return

    # Restart the pod so Odoo cleanly initializes all modules from the new DB
    _restart_odoo_pod(name)
    print(f"[db-create] Fresh DB init complete for {name} — {domain} is ready")


async def _restore_via_odoo(name: str, domain: str, db_template: str, admin_passwd: str):
    """Background task: wait for Odoo to be healthy, then restore database via native endpoint.

    Flow:
    1. Poll GET /web/health on internal K8s service until Odoo responds (up to 10 min)
    2. Download ZIP from Ceph S3
    3. POST to /web/database/restore — Odoo creates+populates the database natively
    4. Fix web.base.url via psql so assets/redirects point to the right domain
    """
    import psycopg2
    from k8s_utils.manifests import PATRONI_HOST, PATRONI_PORT, PATRONI_USER, PATRONI_PASS

    internal_url = f"http://{name}-odoo-svc.odoo-{name}.svc.cluster.local:8069"
    print(f"[restore] Starting background restore for {name} from {db_template}")

    # 1. Wait for Odoo to be reachable (up to 10 min, poll every 15s)
    # In nodb mode (no matching database), Odoo may return 200 on /web/health
    # or redirect to /web/database/selector — both mean Odoo is up and ready.
    await asyncio.sleep(20)  # give Odoo container time to start before first poll
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=False) as http:
        for attempt in range(40):
            try:
                r = await http.get(f"{internal_url}/web/health")
                if r.status_code < 500:   # 200, 302, 303 all mean Odoo is up
                    print(f"[restore] Odoo {name} ready (HTTP {r.status_code}) after {attempt * 15 + 20}s")
                    break
                print(f"[restore] Health check {attempt+1}/40: HTTP {r.status_code} (Odoo still initializing)")
            except Exception as e:
                print(f"[restore] Health check {attempt+1}/40: {type(e).__name__} (not up yet)")
            await asyncio.sleep(15)
        else:
            print(f"[restore] ERROR: Timeout waiting for Odoo {name} — restore aborted")
            return

    # 2. Download ZIP from S3
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            config=Config(signature_version="s3v4"),
            verify=False,
        )
        obj = s3.get_object(Bucket=S3_BUCKET, Key=db_template)
        zip_bytes = obj['Body'].read()
        print(f"[restore] Downloaded {db_template} ({len(zip_bytes) // 1024} KB)")
    except Exception as e:
        print(f"[restore] ERROR: Failed to download {db_template}: {e}")
        return

    # 3. POST to Odoo's native restore endpoint — handles DB creation + filestore
    restore_ok = False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=900.0, write=120.0, pool=5.0)) as http:
            r = await http.post(
                f"{internal_url}/web/database/restore",
                data={"master_pwd": admin_passwd, "name": name, "copy": "true"},
                files={"backup_file": (f"{name}.zip", io.BytesIO(zip_bytes), "application/zip")},
            )
            print(f"[restore] Odoo restore response: HTTP {r.status_code} — {r.text[:200]}")
            restore_ok = True
    except Exception as e:
        # Don't return — the restore may have succeeded despite HTTP errors (large ZIPs
        # can cause Odoo workers to disconnect after the actual restore completes)
        print(f"[restore] WARNING: Restore HTTP request failed: {e}")
        print(f"[restore] Will check if database was created anyway...")

    # 4. Verify the database was actually restored
    # Wait a bit for PostgreSQL to register the new database
    await asyncio.sleep(10 if not restore_ok else 5)
    try:
        conn = psycopg2.connect(
            host=PATRONI_HOST, port=int(PATRONI_PORT),
            user=PATRONI_USER, password=PATRONI_PASS, dbname='postgres'
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
        if not cur.fetchone():
            print(f"[restore] ERROR: Database '{name}' does NOT exist after restore attempt!")
            print(f"[restore] Check master password (admin_passwd) or ZIP file integrity.")
            conn.close()
            # Still restart pod to set proper status
            _restart_odoo_pod(name)
            return
        print(f"[restore] Verified: database '{name}' exists in PostgreSQL")

        # Fix web.base.url
        conn.close()
        conn = psycopg2.connect(
            host=PATRONI_HOST, port=int(PATRONI_PORT),
            user=PATRONI_USER, password=PATRONI_PASS, dbname=name
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "UPDATE ir_config_parameter SET value=%s WHERE key IN (%s,%s)",
            (f"https://{domain}", 'web.base.url', 'web.base.url.static')
        )
        print(f"[restore] Updated web.base.url to https://{domain} ({cur.rowcount} rows)")
        conn.close()
    except Exception as e:
        print(f"[restore] ERROR verifying/configuring DB: {e}")

    # Restart the pod so Odoo cleanly initializes all modules from the restored DB
    _restart_odoo_pod(name)
    print(f"[restore] Restore complete for {name} — {domain} is ready")


# ─── List ──────────────────────────────────────────────────────────────────────

@router.get("")
async def list_instances():
    import json as _json
    core, apps, _ = _k8s()
    namespaces = core.list_namespace(label_selector="managed-by=saas-portal")
    result = []
    for ns in namespaces.items:
        if not ns.metadata.name.startswith("odoo-"):
            continue
        client_name = ns.metadata.name.removeprefix("odoo-")
        pods = core.list_namespaced_pod(ns.metadata.name, label_selector="app=odoo")
        pod_status = "unknown"
        restarts = 0
        if pods.items:
            pod = pods.items[0]
            pod_status = pod.status.phase or "unknown"
            cs = pod.status.container_statuses
            if cs:
                restarts = cs[0].restart_count
        try:
            dep = apps.read_namespaced_deployment(f"{client_name}-odoo", ns.metadata.name)
            annotations = dep.metadata.annotations or {}
            version = dep.metadata.labels.get("odoo-version", "?")
        except Exception:
            annotations, version = {}, "?"

        # Parse addons_repos annotation (JSON list)
        repos_raw = annotations.get("saas/addons-repos", "")
        try:
            repos = _json.loads(repos_raw) if repos_raw else []
        except Exception:
            repos = [{"url": repos_raw, "branch": None}] if repos_raw else []

        result.append({
            "name": client_name,
            "namespace": ns.metadata.name,
            "version": version,
            "pod_status": pod_status,
            "saas_status": annotations.get("saas/status", "ready"),
            "restarts": restarts,
            "protected": annotations.get("saas/protected", "false") == "true",
            "addons_repos": repos,
            "db_template": annotations.get("saas/db-template", ""),
            "image": annotations.get("saas/image", ""),
        })
    return result


# ─── Get Instance ──────────────────────────────────────────────────────────────

@router.get("/{name}")
async def get_instance(name: str):
    import json as _json
    core, apps, _ = _k8s()
    ns = f"odoo-{name}"
    try:
        dep = apps.read_namespaced_deployment(f"{name}-odoo", ns)
    except ApiException:
        raise HTTPException(status_code=404, detail=f"Instance '{name}' not found")

    pods = core.list_namespaced_pod(ns, label_selector="app=odoo")
    pod_info = []
    for pod in pods.items:
        cs = pod.status.container_statuses or []
        pod_info.append({
            "name": pod.metadata.name,
            "phase": pod.status.phase,
            "ready": all(c.ready for c in cs),
            "restarts": sum(c.restart_count for c in cs),
            "node": pod.spec.node_name,
        })

    pvcs = core.list_namespaced_persistent_volume_claim(ns)
    pvc_info = [{"name": p.metadata.name, "status": p.status.phase,
                 "size": p.spec.resources.requests.get("storage")}
                for p in pvcs.items]

    annotations = dep.metadata.annotations or {}
    repos_raw = annotations.get("saas/addons-repos", "")
    try:
        repos = _json.loads(repos_raw) if repos_raw else []
    except Exception:
        repos = [{"url": repos_raw, "branch": None}] if repos_raw else []

    return {
        "name": name,
        "version": dep.metadata.labels.get("odoo-version"),
        "image": annotations.get("saas/image"),
        "protected": annotations.get("saas/protected", "false") == "true",
        "addons_repos": repos,
        "db_template": annotations.get("saas/db-template"),
        "pods": pod_info, "pvcs": pvc_info,
    }


# ─── Get raw odoo.conf ─────────────────────────────────────────────────────────

@router.get("/{name}/config")
async def get_config(name: str):
    core, _, _ = _k8s()
    ns = f"odoo-{name}"
    try:
        cm = core.read_namespaced_config_map(f"{name}-odoo-conf", ns)
        return {"odoo_conf": cm.data.get("odoo.conf", "")}
    except ApiException as e:
        raise HTTPException(status_code=404, detail=str(e))


# ─── Update config (raw odoo.conf or key overrides) ────────────────────────────

@router.patch("/{name}/config")
async def update_config(name: str, body: InstancePatch):
    core, apps, _ = _k8s()
    ns = f"odoo-{name}"
    try:
        cm = core.read_namespaced_config_map(f"{name}-odoo-conf", ns)
        conf = cm.data["odoo.conf"]

        if body.odoo_conf_overrides:
            for key, val in body.odoo_conf_overrides.items():
                conf = re.sub(rf"^{key} = .*$", f"{key} = {val}", conf, flags=re.MULTILINE)
                if f"{key} = {val}" not in conf:
                    conf += f"\n{key} = {val}"

        cm.data["odoo.conf"] = conf
        core.patch_namespaced_config_map(f"{name}-odoo-conf", ns, cm)
    except ApiException as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Handle addons_repos update: save annotation + rebuild initContainers
    if body.addons_repos is not None:
        import json as _json
        try:
            dep = apps.read_namespaced_deployment(f"{name}-odoo", ns)
            repos_json = _json.dumps([{"url": r.url, "branch": r.branch} for r in body.addons_repos])
            if dep.metadata.annotations is None:
                dep.metadata.annotations = {}
            dep.metadata.annotations["saas/addons-repos"] = repos_json

            # Rebuild the sync-addons initContainer from the new repos list
            version = dep.metadata.labels.get("odoo-version", "18")
            image = dep.metadata.annotations.get("saas/image")
            db_template = dep.metadata.annotations.get("saas/db-template")
            new_dep = build_deployment(name, version, image, db_template, body.addons_repos)
            # Replace initContainers in the real deployment
            dep.spec.template.spec.init_containers = new_dep["spec"]["template"]["spec"].get("initContainers", [])
            # Trigger restart with timestamp annotation
            import datetime
            dep.spec.template.metadata.annotations = dep.spec.template.metadata.annotations or {}
            dep.spec.template.metadata.annotations["kubectl.kubernetes.io/restartedAt"] = datetime.datetime.utcnow().isoformat()
            apps.replace_namespaced_deployment(f"{name}-odoo", ns, dep)
        except ApiException as e:
            raise HTTPException(status_code=500, detail=f"Failed to update addons: {e}")

    return {"status": "updated", "restart_required": True}



@router.put("/{name}/config")
async def replace_config(name: str, odoo_conf: str = Body(..., media_type="text/plain")):
    """Replace the entire odoo.conf with raw text content."""
    core, _, _ = _k8s()
    ns = f"odoo-{name}"
    try:
        cm = core.read_namespaced_config_map(f"{name}-odoo-conf", ns)
        cm.data["odoo.conf"] = odoo_conf
        core.patch_namespaced_config_map(f"{name}-odoo-conf", ns, cm)
    except ApiException as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "updated", "restart_required": True}


# ─── Delete Protection ─────────────────────────────────────────────────────────

@router.patch("/{name}/protect")
async def toggle_protection(name: str, protect: bool = True):
    """Set or clear the saas/protected annotation on the deployment."""
    _, apps, _ = _k8s()
    ns = f"odoo-{name}"
    patch = {"metadata": {"annotations": {"saas/protected": "true" if protect else "false"}}}
    try:
        apps.patch_namespaced_deployment(f"{name}-odoo", ns, patch)
    except ApiException as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"protected": protect}


# ─── Restart ───────────────────────────────────────────────────────────────────

@router.post("/{name}/restart")
async def restart_instance(name: str):
    _, apps, _ = _k8s()
    ns = f"odoo-{name}"
    import datetime
    patch = {"spec": {"template": {"metadata": {"annotations":
        {"kubectl.kubernetes.io/restartedAt": datetime.datetime.utcnow().isoformat()}}}}}
    try:
        apps.patch_namespaced_deployment(f"{name}-odoo", ns, patch)
    except ApiException as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "restarting"}


# ─── Logs ──────────────────────────────────────────────────────────────────────

@router.get("/{name}/logs")
async def get_logs(name: str, lines: int = 50):
    core, _, _ = _k8s()
    ns = f"odoo-{name}"
    pods = core.list_namespaced_pod(ns, label_selector="app=odoo")
    if not pods.items:
        raise HTTPException(status_code=404, detail="No pods found")
    log = core.read_namespaced_pod_log(
        pods.items[0].metadata.name, ns, tail_lines=lines, container="odoo")
    return {"logs": log}


# ─── Addon Disk Usage ──────────────────────────────────────────────────────────

@router.get("/{name}/addons")
async def get_addon_usage(name: str):
    """Return disk usage per subdirectory in /mnt/extra-addons (one entry per cloned repo)."""
    core, _, _ = _k8s()
    ns = f"odoo-{name}"
    pods = core.list_namespaced_pod(ns, label_selector="app=odoo")
    if not pods.items:
        raise HTTPException(status_code=404, detail="No pods running")
    pod_name = pods.items[0].metadata.name

    exec_command = ["/bin/sh", "-c",
        "du -h --max-depth=2 /mnt/extra-addons/ 2>/dev/null | sort -rh || echo 'empty'"]
    from kubernetes.stream import stream
    resp = stream(
        core.connect_get_namespaced_pod_exec,
        pod_name, ns,
        command=exec_command,
        container="odoo",
        stderr=True, stdin=False, stdout=True, tty=False,
    )
    lines = []
    for line in resp.strip().split("\n"):
        parts = line.split("\t", 1)
        if len(parts) == 2:
            size, path = parts
            lines.append({"size": size, "path": path, "name": path.split("/")[-1]})
    return {"addons": lines}


# ─── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/{name}", status_code=204)
async def delete_instance(name: str, domain: str = "", force: bool = False):
    _, apps, _ = _k8s()
    core, _, _ = _k8s()
    ns = f"odoo-{name}"

    # Check delete protection
    if not force:
        try:
            dep = apps.read_namespaced_deployment(f"{name}-odoo", ns)
            annotations = dep.metadata.annotations or {}
            if annotations.get("saas/protected") == "true":
                raise HTTPException(
                    status_code=423,
                    detail=f"Instance '{name}' is protected. Use force=true or unlock it first."
                )
        except ApiException as e:
            if e.status != 404:
                pass

    # Drop the PostgreSQL database and role BEFORE removing the namespace
    # (so we can still access the K8s secret for any needed info)
    await _drop_pg_resources(name)

    try:
        core.delete_namespace(ns)
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(status_code=500, detail=str(e))
    if domain:
        await _remove_cloudflare(domain)
    return None
