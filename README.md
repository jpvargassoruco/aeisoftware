Deployment Walkthrough: Aeisoftware SaaS Manager
The Docker architecture for the Master instance and the GitOps pipeline are now fully implemented. Here are the steps to deploy the application on your VM (or locally).

Prerequisites
Ubuntu server with Docker and Docker Compose installed.
The Cloudflare Tunnel details ready.
1. Clone & Start the Master Instance
SSH into your VM and run the following commands:

bash
# Clone the repository
git clone https://github.com/jpvargassoruco/aeisoftware.git
cd aeisoftware
# Export your Cloudflare configuration
export CF_API_TOKEN="feVXj6XvlWKfhJNNiAFJmaA-MD_4G_iYaXEfY0K_" # Your actual token
export CF_ACCOUNT_ID="2755b41b811dc9afc7396ed5d1e27644"
export CF_ZONE_ID="8a6fcaacd01aa1d8e544c85df5a88c8c"
export CF_TUNNEL_ID="b779b85b-eae4-4939-b96d-daecb164c026"
# Launch the Master Instance
sudo docker-compose up -d --build
What this does:

Spins up a Postgres 15 database (db).
Builds and spins up the Master odoo container (v19.0), installing the required cloudflare_manager Python dependency and mounting the docker.sock to enable Docker-outside-Docker deployments.
Spins up an nginx reverse proxy listening on port 80. Nginx handles standard traffic to Odoo and explicitly pipes /websocket traffic to Odoo's longpolling port (8072).
2. Odoo Setup
Access the Master Manager: Open a browser and navigate to http://<your-server-ip>.
Database Creation: Complete the Odoo initial setup. Ensure that "Demo Data" is checked because micro_saas uses XML data to load templates.
Install Apps: Go to "Apps", update the App List, and install AEI SaaS Manager. (Note: The modules are located in the /addons folder).
3. Provisioning a Child Instance
Navigate to the Odoo Docker Instance module.
Create a new Instance.
Template: Select Odoo 19 (With Proxy). This is the new template I added that configures an Nginx sidecar for the child instance to support its own WebSockets through Cloudflare!
Domain Name: Provide the complete Cloudflare domain you want this assigned to (e.g., test19.aeisoftware.com).
Click Start Instance.
What happens next:

The Master micro_saas app triggers a docker-compose up on the host socket to launch the new child's Postgres, Odoo 19, and Nginx.
Upon a successful start, aei_saas_manager intercepts the process, connects to Cloudflare API with the tokens you provided in step 1, creates a CNAME for test19.aeisoftware.com, and routes it directly to the child's Nginx port.
You can instantly visit test19.aeisoftware.com to access the running child instance!

