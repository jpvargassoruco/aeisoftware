# Aeisoftware SaaS Manager - Odoo 17.0

The Docker architecture for the Master instance and the GitOps pipeline are now fully implemented on Odoo 17.0.

## 1. Quick Start / Deployment

SSH into your VM and run the following commands to start the Master Instance:

```bash
# Clone the repository
git clone git@github.com:jpvargassoruco/aeisoftware.git
cd aeisoftware

# Create the external network (mandatory for Traefik)
sudo docker network create web-proxy

# Launch the Master Instance and Traefik
sudo docker compose up -d --build
```

### What this does:
- Spins up a **Postgres 15** database.
- Builds the **Master Odoo 17.0** container.
- Launches **Traefik** as the reverse proxy for the entire stack.
- Configures automatic routing for the master instance (Port 8069 and 8072 for WebSockets).

## 2. Odoo Setup
1. **Access the Master**: Open `http://master.sdnbo.net` (or your configured domain).
2. **Database Creation**: Create a database. **Important**: Check "Demo Data" to load default templates.
3. **Install Manager**: Go to Apps and install **AEI SaaS Manager**.

## 3. Provisioning Child Instances

You can now use the automated script to create new Odoo instances compatible with Traefik:

```bash
# Usage: ./crear_instancia_odoo.sh <instance_name> <domain>
./crear_instancia_odoo.sh client1 client1.sdnbo.net
```

- This script generates a dedicated directory, `odoo.conf`, and `docker-compose.yml` with the correct Traefik labels.
- It also handles secret generation for PostgreSQL.

---

## Migration Walkthrough (Odoo 19 -> 17.0)

Summary of changes made during the migration:

### Submodule Restructuring
- Relocated `micro_saas` to `addons/micro_saas` (branch 17.0).
- Removed legacy root-level submodule.

### Key Improvements
- **Nginx Sidecars**: Integrated Nginx into child instance templates for optimized routing.
- **Docker Compose V2**: Updated all scripts to use `docker compose`.
- **17.0 Compatibility**: Full update of `aei_saas_manager` and `cloudflare_manager`.
