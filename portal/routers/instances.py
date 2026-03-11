from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import httpx, os
from k8s_utils.manifests import (
    build_namespace, build_secret, build_configmap,
    build_pvcs, build_deployment, build_service, build_ingress,
    SUPPORTED_VERSIONS, CF_API_TOKEN, CF_ACCOUNT_ID, CF_ZONE_ID, CF_TUNNEL_ID,
)

router = APIRouter()


class InstanceCreate(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9][a-z0-9\-]{1,30}$")
    domain: str
    odoo_version: str = Field("18", pattern=r"^(17|18|19)$")
    db_template: Optional[str] = None          # e.g. "v18/starter.dump"
    addons_repo: Optional[str] = None          # e.g. "https://github.com/org/addons.git"
    image: Optional[str] = None                # override image, e.g. "ghcr.io/org/odoo:18"
    db_password: str = Field(default="odoo")
    odoo_conf_overrides: dict = Field(default_factory=dict)


class InstancePatch(BaseModel):
    odoo_conf_overrides: Optional[dict] = None
    addons_repo: Optional[str] = None


def _k8s():
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api(), client.NetworkingV1Api()


def _apply(api_func, body, name, namespace=None):
    """Generic apply — create or update."""
    try:
        if namespace:
            api_func.create_namespaced_resource(namespace=namespace, body=body)
        else:
            api_func.create_resource(body=body)
    except ApiException as e:
        if e.status == 409:  # already exists
            pass
        else:
            raise


async def _configure_cloudflare(name: str, domain: str):
    """Add Cloudflare DNS CNAME + Tunnel ingress route."""
    if not all([CF_API_TOKEN, CF_ZONE_ID, CF_TUNNEL_ID]):
        return
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}
    tunnel_domain = f"{CF_TUNNEL_ID}.cfargotunnel.com"
    async with httpx.AsyncClient() as http:
        # DNS CNAME
        await http.post(
            f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records",
            headers=headers,
            json={"type": "CNAME", "name": domain, "content": tunnel_domain,
                  "proxied": True, "ttl": 1},
        )
        # Tunnel route — get current config and append
        r = await http.get(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations",
            headers=headers,
        )
        current = r.json().get("result", {}).get("config", {})
        ingress = current.get("ingress", [{"service": "http_status:404"}])
        # Insert before catch-all
        new_route = {"hostname": domain, "service": "http://traefik.kube-system.svc.cluster.local:80"}
        ingress_no_catch = [i for i in ingress if i.get("hostname")]
        catch_all = [i for i in ingress if not i.get("hostname")]
        ingress_updated = ingress_no_catch + [new_route] + catch_all
        await http.put(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations",
            headers=headers,
            json={"config": {"ingress": ingress_updated}},
        )


async def _remove_cloudflare(domain: str):
    if not all([CF_API_TOKEN, CF_ZONE_ID, CF_TUNNEL_ID]):
        return
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient() as http:
        # Remove DNS record
        r = await http.get(
            f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records?name={domain}",
            headers=headers,
        )
        for record in r.json().get("result", []):
            await http.delete(
                f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records/{record['id']}",
                headers=headers,
            )
        # Remove from tunnel config
        r2 = await http.get(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations",
            headers=headers,
        )
        current = r2.json().get("result", {}).get("config", {})
        ingress = [i for i in current.get("ingress", []) if i.get("hostname") != domain]
        await http.put(
            f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/cfd_tunnel/{CF_TUNNEL_ID}/configurations",
            headers=headers,
            json={"config": {"ingress": ingress}},
        )


@router.post("", status_code=201)
async def create_instance(body: InstanceCreate):
    core, apps, net = _k8s()
    ns = f"odoo-{body.name}"

    # Create namespace
    try:
        core.create_namespace(build_namespace(body.name))
    except ApiException as e:
        if e.status != 409:
            raise HTTPException(status_code=500, detail=str(e))

    # Apply all resources
    try:
        core.create_namespaced_secret(ns, build_secret(body.name, body.db_password))
    except ApiException as e:
        if e.status != 409:
            raise HTTPException(status_code=500, detail=str(e))

    try:
        core.create_namespaced_config_map(ns, build_configmap(
            body.name, body.domain, body.db_password, body.odoo_conf_overrides))
    except ApiException as e:
        if e.status != 409:
            raise HTTPException(status_code=500, detail=str(e))

    for pvc in build_pvcs(body.name):
        try:
            core.create_namespaced_persistent_volume_claim(ns, pvc)
        except ApiException as e:
            if e.status != 409:
                raise HTTPException(status_code=500, detail=str(e))

    deployment = build_deployment(
        body.name, body.odoo_version, body.image, body.db_template, body.addons_repo)
    try:
        apps.create_namespaced_deployment(ns, deployment)
    except ApiException as e:
        if e.status != 409:
            raise HTTPException(status_code=500, detail=str(e))

    try:
        core.create_namespaced_service(ns, build_service(body.name))
    except ApiException as e:
        if e.status != 409:
            raise HTTPException(status_code=500, detail=str(e))

    try:
        net.create_namespaced_ingress(ns, build_ingress(body.name, body.domain))
    except ApiException as e:
        if e.status != 409:
            raise HTTPException(status_code=500, detail=str(e))

    # Configure Cloudflare
    await _configure_cloudflare(body.name, body.domain)

    return {"name": body.name, "domain": body.domain,
            "url": f"https://{body.domain}", "status": "provisioning"}


@router.get("")
async def list_instances():
    core, apps, _ = _k8s()
    namespaces = core.list_namespace(label_selector="managed-by=saas-portal")
    result = []
    for ns in namespaces.items:
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
        result.append({
            "name": client_name,
            "namespace": ns.metadata.name,
            "version": version,
            "pod_status": pod_status,
            "restarts": restarts,
            "addons_repo": annotations.get("saas/addons-repo", ""),
            "db_template": annotations.get("saas/db-template", ""),
            "image": annotations.get("saas/image", ""),
        })
    return result


@router.get("/{name}")
async def get_instance(name: str):
    core, apps, _ = _k8s()
    ns = f"odoo-{name}"
    try:
        dep = apps.read_namespaced_deployment(f"{name}-odoo", ns)
    except ApiException as e:
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
    return {
        "name": name, "version": dep.metadata.labels.get("odoo-version"),
        "image": annotations.get("saas/image"),
        "addons_repo": annotations.get("saas/addons-repo"),
        "db_template": annotations.get("saas/db-template"),
        "pods": pod_info, "pvcs": pvc_info,
    }


@router.patch("/{name}/config")
async def update_config(name: str, body: InstancePatch):
    core, apps, _ = _k8s()
    ns = f"odoo-{name}"
    if body.odoo_conf_overrides:
        try:
            cm = core.read_namespaced_config_map(f"{name}-odoo-conf", ns)
            conf = cm.data["odoo.conf"]
            for key, val in body.odoo_conf_overrides.items():
                import re
                conf = re.sub(rf"^{key} = .*$", f"{key} = {val}", conf, flags=re.MULTILINE)
                if f"{key} = {val}" not in conf:
                    conf += f"\n{key} = {val}"
            cm.data["odoo.conf"] = conf
            core.patch_namespaced_config_map(f"{name}-odoo-conf", ns, cm)
        except ApiException as e:
            raise HTTPException(status_code=404, detail=str(e))
    return {"status": "updated", "restart_required": True}


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


@router.delete("/{name}", status_code=204)
async def delete_instance(name: str, domain: str = ""):
    core, _, _ = _k8s()
    ns = f"odoo-{name}"
    try:
        core.delete_namespace(ns)
    except ApiException as e:
        if e.status != 404:
            raise HTTPException(status_code=500, detail=str(e))
    if domain:
        await _remove_cloudflare(domain)
    return None
