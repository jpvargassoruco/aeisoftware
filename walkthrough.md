# Deployment & Migration Walkthrough

This document covers both the deployment of the Master Instance and the technical details of the Odoo 17.0 migration.

## 1. Master Instance Deployment

SSH into your VM and run the following:

```bash
# Export Cloudflare configuration
export CF_API_TOKEN="your_token"
export CF_ACCOUNT_ID="your_id"
export CF_ZONE_ID="your_zone"
export CF_TUNNEL_ID="your_tunnel"

# Start the Master
sudo docker compose up -d --build
```

## 2. Migration Details (Odoo 19 -> 17.0)

Done on 2026-03-05:
- **Submode Update**: Switched `micro_saas` to 17.0 branch in `addons/micro_saas`.
- **Docker Fixes**: Removed `--break-system-packages` from Dockerfile.
- **Sidecar Support**: Added automated Nginx sidecar config for child instances.
- **Manager Update**: Verified `aei_saas_manager` compatibility with Odoo 17.0.

## 3. Template Management
New templates added:
- Odoo 17 (Nginx Sidecar)
- Odoo 18 (Nginx Sidecar)
- Odoo 19 (Nginx Sidecar)

These templates ensure that each child instance has proper routing through its own Nginx container.
