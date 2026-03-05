# Aeisoftware SaaS Manager - Odoo 17.0

The Docker architecture for the Master instance and the GitOps pipeline are now fully implemented on Odoo 17.0.

## 1. Quick Start / Deployment

SSH into your VM and run the following commands to start the Master Instance:

```bash
# Clone the repository (if not already present)
# git clone https://github.com/jpvargassoruco/aeisoftware.git
# cd aeisoftware

# Export your Cloudflare configuration
export CF_API_TOKEN="your_cloudflare_token"
export CF_ACCOUNT_ID="your_account_id"
export CF_ZONE_ID="your_zone_id"
export CF_TUNNEL_ID="your_tunnel_id"

# Launch the Master Instance
sudo docker compose up -d --build
```

### What this does:
- Spins up a **Postgres 15** database.
- Builds the **Master Odoo 17.0** container with required dependencies (`requests`, `docker-cli`).
- Sets up an **Nginx reverse proxy** to handle standard traffic and WebSockets for the Master.

## 2. Odoo Setup
1. **Access the Master**: Open `http://<your-server-ip>`.
2. **Database Creation**: Create a database. **Important**: Check "Demo Data" to load default templates.
3. **Install Manager**: Go to Apps and install **AEI SaaS Manager**.

## 3. Provisioning Child Instances
1. Navigate to **Instance Management** -> **Odoo Instances**.
2. Create a new instance and select a template:
   - **Odoo 17/18/19 (Nginx Sidecar)**: Recommended for full WebSocket support.
3. Enter the **Domain Name** (e.g., `client1.aeisoftware.com`).
4. Click **Start Instance**.
   - This launches child containers and automatically configures Cloudflare.

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
