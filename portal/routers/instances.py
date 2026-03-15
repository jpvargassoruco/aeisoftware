"""
Odoo SaaS instance management — optimized for performance.

Key optimizations over the original:
  - K8s client singleton (avoids re-parsing SA token per request)
  - list_instances uses 2 cluster-wide calls instead of O(N) per-namespace calls
  - ZIP restore streams via tempfile (avoids 2× full ZIP in RAM)
  - S3 client singleton (avoids boto3 credential resolution per call)
  - Domain duplicate check uses single cluster-wide ingress list
  - Module-level imports (psycopg2, json, datetime) instead of per-function
"""
from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel, Field
from typing import Optional, List
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import httpx, os, re, asyncio, io, json, datetime, tempfile, psycopg2, secrets
from psycopg2 import sql
import boto3
from botocore.client import Config
from k8s_utils.manifests import (
    build_namespace, build_secret, build_configmap,
    build_pvcs, build_deployment, build_service, build_ingress,
    build_limitrange, build_resourcequota, build_pdb,
    SUPPORTED_VERSIONS, CF_API_TOKEN, CF_ACCOUNT_ID, CF_ZONE_ID, CF_TUNNEL_ID,
    S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET,
    PATRONI_HOST, PATRONI_PORT, PATRONI_USER, PATRONI_PASS,
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


# ─── Singletons — avoid recreating clients per request ─────────────────────────

_k8s_core: client.CoreV1Api | None = None
_k8s_apps: client.AppsV1Api | None = None
_k8s_net: client.NetworkingV1Api | None = None


def _k8s():
    """Return cached K8s API clients (singleton pattern).

    Performance: avoids re-parsing the ServiceAccount token and creating
    3 new client objects on every request (~50ms saved per call).
    """
    global _k8s_core, _k8s_apps, _k8s_net
    if _k8s_core is None:
        try:
            config.load_incluster_config()
        except Exception:
            config.load_kube_config()
        _k8s_core = client.CoreV1Api()
        _k8s_apps = client.AppsV1Api()
        _k8s_net = client.NetworkingV1Api()
    return _k8s_core, _k8s_apps, _k8s_net


_s3_client = None


def _s3():
    """Return cached S3 client (singleton pattern)."""
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            config=Config(signature_version="s3v4"),
            verify=False,
        )
    return _s3_client


# ─── Helpers ───────────────────────────────────────────────────────────────────

async def _drop_pg_resources(name: str):
    """Drop ALL PostgreSQL databases belonging to this instance on deletion."""
    try:
        conn = psycopg2.connect(
            host=PATRONI_HOST, port=int(PATRONI_PORT),
            user=PATRONI_USER, password=PATRONI_PASS,
            dbname="postgres", connect_timeout=10
        )
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute(
            "SELECT datname FROM pg_database "
            "WHERE datistemplate = false AND datname LIKE %s",
            (name + "%",)
        )
        databases = [row[0] for row in cur.fetchall()]
        print(f"[portal] Databases to drop for instance '{name}': {databases}")

        for dbname in databases:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (dbname,)
            )
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(dbname)))
            print(f"[portal] Dropped database: {dbname}")

        cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
        conn.close()
        print(f"[portal] Cleaned up all PostgreSQL resources for instance: {name}")
    except Exception as e:
        print(f"[portal] Warning: could not drop PostgreSQL resources for {name}: {e}")


def _safe_create(fn, *args, **kwargs):
    """Create K8s resource, ignore 409 AlreadyExists."""
    try:
        fn(*args, **kwargs)
    except ApiException as e:
        if e.status != 409:
            raise HTTPException(status_code=500, detail=str(e))


def _repos_annotation(repos: List[AddonRepo]) -> str:
    return json.dumps([{"url": r.url, "branch": r.branch} for r in repos])


async def _configure_cloudflare(name: str, domain: str):
    # ── OPTIMIZATION (Priority #2 — 200-instance plan) ──────────────────────
    # Per-instance DNS CNAME + tunnel ingress calls removed.
    # A single wildcard rule (*.aeisoftware.com → Traefik) now covers all
    # subdomains. Run setup_wildcard_tunnel.py once to provision it.
    # The Ingress object created above (build_ingress) is all that's needed;
    # Traefik routes by Host header to the correct namespace.
    print(f"[cf] Wildcard tunnel active — no per-instance config needed for {domain}")


async def _remove_cloudflare(domain: str):
    # ── OPTIMIZATION (Priority #2 — 200-instance plan) ──────────────────────
    # Per-instance tunnel route removal is a no-op with the wildcard setup.
    # The wildcard *.aeisoftware.com route stays; Traefik Ingress deletion
    # (done above in the delete endpoint) is sufficient — 404s for the
    # deleted subdomain are handled by the wildcard catch-all.
    print(f"[cf] Wildcard tunnel active — no per-instance cleanup needed for {domain}")


def _create_pg_user(name: str) -> tuple[str, str]:
    """Create a per-instance PostgreSQL role.

    Returns (db_user, db_password).  The role is granted LOGIN, NOSUPERUSER,
    CREATEDB (so Odoo can create migration temp-DBs if needed), and NOREPLICATION.
    On collision (role already exists) the password is simply reset.
    """
    db_user = name.replace("-", "_")       # PG identifiers can't have hyphens
    db_password = secrets.token_urlsafe(24) # 32-char URL-safe password
    try:
        conn = psycopg2.connect(
            host=PATRONI_HOST, port=int(PATRONI_PORT),
            user=PATRONI_USER, password=PATRONI_PASS,
            dbname="postgres", connect_timeout=10,
        )
        conn.autocommit = True
        cur = conn.cursor()
        # Check if the role already exists
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (db_user,))
        if cur.fetchone():
            cur.execute(
                sql.SQL("ALTER ROLE {} WITH PASSWORD %s").format(sql.Identifier(db_user)),
                (db_password,),
            )
            print(f"[pg-user] Role '{db_user}' already exists — password reset")
        else:
            cur.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER CREATEDB NOREPLICATION PASSWORD %s"
                ).format(sql.Identifier(db_user)),
                (db_password,),
            )
            print(f"[pg-user] Created PG role: {db_user}")
        conn.close()
    except Exception as e:
        print(f"[pg-user] ERROR creating PG role for {name}: {e}")
        raise
    return db_user, db_password


def _transfer_db_ownership(name: str, db_user: str):
    """Transfer ownership of database ``name`` to ``db_user``.

    Also grants usage/create on the public schema and reassigns owned objects
    inside the database so the per-instance role can ALTER tables during
    Odoo module upgrades.
    """
    try:
        conn = psycopg2.connect(
            host=PATRONI_HOST, port=int(PATRONI_PORT),
            user=PATRONI_USER, password=PATRONI_PASS,
            dbname="postgres", connect_timeout=10,
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(name), sql.Identifier(db_user)
            )
        )
        conn.close()

        # Inside the target DB: reassign objects + ensure schema perms
        conn = psycopg2.connect(
            host=PATRONI_HOST, port=int(PATRONI_PORT),
            user=PATRONI_USER, password=PATRONI_PASS,
            dbname=name, connect_timeout=10,
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            sql.SQL("REASSIGN OWNED BY {} TO {}").format(
                sql.Identifier(PATRONI_USER), sql.Identifier(db_user)
            )
        )
        cur.execute(
            sql.SQL("GRANT ALL ON SCHEMA public TO {}").format(
                sql.Identifier(db_user)
            )
        )
        conn.close()
        print(f"[pg-user] Transferred DB '{name}' ownership to '{db_user}'")
    except Exception as e:
        print(f"[pg-user] WARNING: ownership transfer failed for {name}: {e}")


# ─── Create ────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_instance(body: InstanceCreate):
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

    # ── Pre-flight: duplicate domain check (single cluster-wide call) ───────────
    # OPTIMIZATION: replaced O(N) per-namespace ingress reads with 1 cluster-wide list
    all_ingresses = net.list_ingress_for_all_namespaces(
        label_selector="app=odoo"
    )
    for ing in all_ingresses.items:
        for rule in (ing.spec.rules or []):
            if rule.host == body.domain:
                owner = ing.metadata.namespace.removeprefix("odoo-")
                raise HTTPException(
                    status_code=409,
                    detail=f"Domain '{body.domain}' is already in use by instance '{owner}'."
                )

    _safe_create(core.create_namespace, build_namespace(body.name))

    # ── Per-instance PostgreSQL role ─────────────────────────────────────────
    instance_db_user, instance_db_pass = _create_pg_user(body.name)

    _safe_create(core.create_namespaced_secret, ns,
                 build_secret(body.name, body.admin_passwd,
                              instance_db_user, instance_db_pass))
    _safe_create(core.create_namespaced_config_map, ns,
                 build_configmap(body.name, body.domain, body.admin_passwd,
                                body.odoo_conf_overrides, body.addons_repos,
                                instance_db_user, instance_db_pass))

    for pvc in build_pvcs(body.name):
        _safe_create(core.create_namespaced_persistent_volume_claim, ns, pvc)

    deployment = build_deployment(
        body.name, body.odoo_version, body.image,
        body.db_template, body.addons_repos)
    _safe_create(apps.create_namespaced_deployment, ns, deployment)
    _safe_create(core.create_namespaced_service, ns, build_service(body.name))
    _safe_create(net.create_namespaced_ingress, ns, build_ingress(body.name, body.domain))

    # ── Namespace guardrails ─────────────────────────────────────────────────
    _safe_create(core.create_namespaced_limit_range, ns, build_limitrange(body.name))
    _safe_create(core.create_namespaced_resource_quota, ns, build_resourcequota(body.name))
    from kubernetes.client import PolicyV1Api
    policy = PolicyV1Api()
    _safe_create(policy.create_namespaced_pod_disruption_budget, ns, build_pdb(body.name))

    cf_warning = None
    try:
        await _configure_cloudflare(body.name, body.domain)
    except Exception as cf_err:
        cf_warning = str(cf_err)
        print(f"[cf] WARNING: could not configure Cloudflare for {body.domain}: {cf_err}")

    # ── Spawn background task to initialize the database via Odoo native API ──
    if body.db_template:
        asyncio.create_task(_restore_via_odoo(
            name=body.name,
            domain=body.domain,
            db_template=body.db_template,
            admin_passwd=body.admin_passwd,
            instance_db_user=instance_db_user,
        ))
    else:
        asyncio.create_task(_initialize_fresh_db(
            name=body.name,
            domain=body.domain,
            admin_passwd=body.admin_passwd,
            admin_email=body.admin_email,
            admin_password=body.admin_password,
            lang=body.lang,
            instance_db_user=instance_db_user,
        ))

    return {"name": body.name, "domain": body.domain,
            "url": f"https://{body.domain}", "status": "provisioning",
            "cf_warning": cf_warning}


def _restart_odoo_pod(name: str, core=None, apps=None):
    """Restart Odoo deployment + set status=ready.

    Accepts optional pre-existing K8s clients to avoid singleton re-lookup.
    """
    if core is None or apps is None:
        core, apps, _ = _k8s()
    ns = f"odoo-{name}"

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
    admin_email: str, admin_password: str, lang: str = "en_US",
    instance_db_user: str | None = None,
):
    """Background task: initialize a fresh Odoo database via /web/database/create."""
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

    # Call /web/database/create
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

    # Verify the database was actually created
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
            print(f"[db-create] ERROR: Database '{name}' was NOT created!")
            conn.close()
            return
        print(f"[db-create] Verified: database '{name}' exists in PostgreSQL")

        # Fix web.base.url (reuse connection to postgres, reopen to target DB)
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

    # Transfer DB ownership to per-instance PG role
    if instance_db_user:
        _transfer_db_ownership(name, instance_db_user)

    _restart_odoo_pod(name)
    print(f"[db-create] Fresh DB init complete for {name} — {domain} is ready")


async def _restore_via_odoo(name: str, domain: str, db_template: str, admin_passwd: str,
                            instance_db_user: str | None = None):
    """Background task: restore database via Odoo's /web/database/restore.

    OPTIMIZATION: Uses tempfile for ZIP instead of loading entirely into RAM.
    Original: 2× ZIP size in memory (read + BytesIO copy).
    Optimized: ~0 extra RAM — streamed to disk, then uploaded from disk.
    """
    internal_url = f"http://{name}-odoo-svc.odoo-{name}.svc.cluster.local:8069"
    print(f"[restore] Starting background restore for {name} from {db_template}")

    # 1. Wait for Odoo to be reachable
    await asyncio.sleep(20)
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), follow_redirects=False) as http:
        for attempt in range(40):
            try:
                r = await http.get(f"{internal_url}/web/health")
                if r.status_code < 500:
                    print(f"[restore] Odoo {name} ready (HTTP {r.status_code}) after {attempt * 15 + 20}s")
                    break
                print(f"[restore] Health check {attempt+1}/40: HTTP {r.status_code} (Odoo still initializing)")
            except Exception as e:
                print(f"[restore] Health check {attempt+1}/40: {type(e).__name__} (not up yet)")
            await asyncio.sleep(15)
        else:
            print(f"[restore] ERROR: Timeout waiting for Odoo {name} — restore aborted")
            return

    # 2. Download ZIP from S3 to tempfile (OPTIMIZATION: no full ZIP in RAM)
    tmp_path = None
    try:
        s3 = _s3()
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp_path = tmp.name
            s3.download_fileobj(S3_BUCKET, db_template, tmp)
        file_size = os.path.getsize(tmp_path)
        print(f"[restore] Downloaded {db_template} to disk ({file_size // 1024} KB)")
    except Exception as e:
        print(f"[restore] ERROR: Failed to download {db_template}: {e}")
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return

    # 3. POST to Odoo's native restore endpoint — stream from disk
    restore_ok = False
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=900.0, write=120.0, pool=5.0)) as http:
            with open(tmp_path, "rb") as f:
                r = await http.post(
                    f"{internal_url}/web/database/restore",
                    data={"master_pwd": admin_passwd, "name": name, "copy": "true"},
                    files={"backup_file": (f"{name}.zip", f, "application/zip")},
                )
            print(f"[restore] Odoo restore response: HTTP {r.status_code} — {r.text[:200]}")
            restore_ok = True
    except Exception as e:
        print(f"[restore] WARNING: Restore HTTP request failed: {e}")
        print(f"[restore] Will check if database was created anyway...")
    finally:
        # Clean up tempfile
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    # 4. Verify the database was actually restored
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
            conn.close()
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

    # Transfer DB ownership to per-instance PG role
    if instance_db_user:
        _transfer_db_ownership(name, instance_db_user)

    _restart_odoo_pod(name)
    print(f"[restore] Restore complete for {name} — {domain} is ready")


# ─── List ──────────────────────────────────────────────────────────────────────

@router.get("")
async def list_instances(
    page: int = 1,
    page_size: int = 50,
):
    """List all SaaS instances (paginated).

    OPTIMIZATION: Uses 3 cluster-wide API calls instead of O(N) per-namespace calls.
    Pagination defaults to 50 items per page to keep response times low at scale.
    """
    core, apps, _ = _k8s()
    namespaces = core.list_namespace(label_selector="managed-by=saas-portal")

    # Build set of managed namespace names for filtering
    managed_ns = {}
    for ns in namespaces.items:
        if ns.metadata.name.startswith("odoo-"):
            managed_ns[ns.metadata.name] = ns.metadata.name.removeprefix("odoo-")

    if not managed_ns:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    # 2 cluster-wide calls instead of 2×N per-namespace calls
    all_pods = core.list_pod_for_all_namespaces(label_selector="app=odoo")
    all_deploys = apps.list_deployment_for_all_namespaces(label_selector="app=odoo")

    # Index pods by namespace (first pod per namespace)
    pods_by_ns: dict = {}
    for pod in all_pods.items:
        ns_name = pod.metadata.namespace
        if ns_name in managed_ns and ns_name not in pods_by_ns:
            pods_by_ns[ns_name] = pod

    # Index deployments by namespace
    deploys_by_ns: dict = {}
    for dep in all_deploys.items:
        ns_name = dep.metadata.namespace
        if ns_name in managed_ns:
            deploys_by_ns[ns_name] = dep

    # Sort for stable pagination
    sorted_ns = sorted(managed_ns.items(), key=lambda x: x[1])
    total = len(sorted_ns)

    # Paginate
    start = (page - 1) * page_size
    end = start + page_size
    page_ns = sorted_ns[start:end]

    result = []
    for ns_name, client_name in page_ns:
        # Pod info
        pod = pods_by_ns.get(ns_name)
        pod_status = "unknown"
        restarts = 0
        if pod:
            pod_status = pod.status.phase or "unknown"
            cs = pod.status.container_statuses
            if cs:
                restarts = cs[0].restart_count

        # Deployment info
        dep = deploys_by_ns.get(ns_name)
        annotations = {}
        version = "?"
        if dep:
            annotations = dep.metadata.annotations or {}
            version = dep.metadata.labels.get("odoo-version", "?")

        # Parse addons_repos annotation (JSON list)
        repos_raw = annotations.get("saas/addons-repos", "")
        try:
            repos = json.loads(repos_raw) if repos_raw else []
        except Exception:
            repos = [{"url": repos_raw, "branch": None}] if repos_raw else []

        result.append({
            "name": client_name,
            "namespace": ns_name,
            "version": version,
            "pod_status": pod_status,
            "saas_status": annotations.get("saas/status", "ready"),
            "restarts": restarts,
            "protected": annotations.get("saas/protected", "false") == "true",
            "addons_repos": repos,
            "db_template": annotations.get("saas/db-template", ""),
            "image": annotations.get("saas/image", ""),
        })
    return {"items": result, "total": total, "page": page, "page_size": page_size}


# ─── Get Instance ──────────────────────────────────────────────────────────────

@router.get("/{name}")
async def get_instance(name: str):
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
        repos = json.loads(repos_raw) if repos_raw else []
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

    # Handle addons_repos update
    if body.addons_repos is not None:
        try:
            dep = apps.read_namespaced_deployment(f"{name}-odoo", ns)
            repos_json = json.dumps([{"url": r.url, "branch": r.branch} for r in body.addons_repos])
            if dep.metadata.annotations is None:
                dep.metadata.annotations = {}
            dep.metadata.annotations["saas/addons-repos"] = repos_json

            version = dep.metadata.labels.get("odoo-version", "18")
            image = dep.metadata.annotations.get("saas/image")
            db_template = dep.metadata.annotations.get("saas/db-template")
            new_dep = build_deployment(name, version, image, db_template, body.addons_repos)
            dep.spec.template.spec.init_containers = new_dep["spec"]["template"]["spec"].get("initContainers", [])
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
    """Return disk usage per subdirectory in /mnt/extra-addons."""
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
    core, apps, _ = _k8s()  # Single call instead of 2× _k8s()
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
        except HTTPException:
            raise
        except ApiException as e:
            if e.status != 404:
                pass

    await _drop_pg_resources(name)

    try:
        core.delete_namespace(ns)
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(status_code=500, detail=str(e))
    if domain:
        await _remove_cloudflare(domain)
    return None
