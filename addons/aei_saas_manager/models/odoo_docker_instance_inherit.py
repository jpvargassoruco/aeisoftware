import logging
import os
from odoo import models, fields, api
from odoo.exceptions import UserError
try:
    # Requires the cloudflare_manager package to be in the PYTHONPATH or installed
    from cloudflare_manager.client import CloudflareTunnelManager
except ImportError:
    CloudflareTunnelManager = None

_logger = logging.getLogger(__name__)

class OdooDockerInstanceCF(models.Model):
    _inherit = 'odoo.docker.instance'

    domain_name = fields.Char(string='Public Domain Name', help="e.g. test.yourdomain.com", required=True)
    cloudflare_status = fields.Selection([
        ('pending', 'Pending'),
        ('published', 'Published'),
        ('failed', 'Failed')
    ], string='Cloudflare Status', default='pending', readonly=True)

    @api.depends('name')
    def _compute_user_path(self):
        for instance in self:
            if not instance.name:
                continue
            # Force /home/ubuntu to ensure paths are consistent between container and host
            instance.user_path = '/home/ubuntu'
            instance.instance_data_path = os.path.join(instance.user_path, 'odoo_docker', 'data',
                                                       instance.name.replace('.', '_').replace(' ', '_').lower())
            # Regenerate result_dc_body to ensure any path-dependent variables are updated
            instance.result_dc_body = self._get_formatted_body(template_body=instance.template_dc_body,
                                                               demo_fallback=True)

    def _get_cf_manager(self):
        # We fetch the params from the system environment or odoo config
        # For this PoC, we expect them to be in the environment where Odoo is running
        api_token = os.environ.get('CF_API_TOKEN')
        account_id = os.environ.get('CF_ACCOUNT_ID')
        zone_id = os.environ.get('CF_ZONE_ID')

        if not api_token or not account_id or not zone_id:
            raise UserError("Cloudflare configuration is missing in environment variables (CF_API_TOKEN, CF_ACCOUNT_ID, CF_ZONE_ID).")

        if not CloudflareTunnelManager:
            raise UserError("CloudflareTunnelManager python module failed to import.")
            
        return CloudflareTunnelManager(api_token, account_id, zone_id)

    def _create_nginx_conf(self):
        for instance in self:
            nginx_conf_path = os.path.join(instance.instance_data_path, 'nginx.conf')
            # Determine the Odoo service name from the template if possible, 
            # but for our templates we will use 'odoo'
            nginx_conf_content = """events {
    worker_connections 1024;
}
http {
    upstream odoo_backend {
        server odoo:8069;
    }
    upstream odoo_livechat {
        server odoo:8072;
    }
    server {
        listen 80;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_buffers 16 64k;
        proxy_buffer_size 128k;

        location /websocket {
            proxy_pass http://odoo_livechat;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
        }
        location / {
            proxy_pass http://odoo_backend;
        }
    }
}
"""
            try:
                instance._makedirs(os.path.dirname(nginx_conf_path))
                with open(nginx_conf_path, "w") as f:
                    f.write(nginx_conf_content)
                instance.add_to_log(f"[INFO] Nginx configuration created at {nginx_conf_path}")
            except Exception as e:
                instance.add_to_log(f"[ERROR] Failed to create Nginx config: {str(e)}")

    def start_instance(self):
        # Create Nginx config before starting
        self._create_nginx_conf()
        
        # First, call the original method to start the container
        super(OdooDockerInstanceCF, self).start_instance()
        
        # If it started successfully, we publish it to Cloudflare
        for instance in self:
            if instance.state == 'running':
                instance.add_to_log("[INFO] Publishing to Cloudflare Tunnel...")
                try:
                    cf_manager = self._get_cf_manager()
                    tunnel_id = os.environ.get('CF_TUNNEL_ID')
                    if not tunnel_id:
                        raise UserError("CF_TUNNEL_ID is not set in environment.")
                    
                    # 1. Add route to the tunnel
                    # Cloudflared container must be able to resolve 'localhost:port' or standard IP
                    # Assuming localhost works for the host network or we pass the host IP
                    # For a basic setup, relying on the HTTP port mapped to the host
                    service_url = f"http://localhost:{instance.http_port}"
                    route_added = cf_manager.add_route_to_tunnel(tunnel_id, instance.domain_name, service_url)
                    
                    # 2. Add DNS CNAME record
                    # We extract the first part of the domain, e.g. "test" from "test.yourdomain.com"
                    subdomain = instance.domain_name.split('.')[0]
                    dns_added = cf_manager.create_dns_cname(subdomain, tunnel_id)

                    if route_added and dns_added:
                        instance.write({'cloudflare_status': 'published'})
                        instance.add_to_log(f"[INFO] Successfully published at https://{instance.domain_name}")
                    else:
                        instance.write({'cloudflare_status': 'failed'})
                        instance.add_to_log("[ERROR] Cloudflare publishing failed. Check logs.")

                except Exception as e:
                    instance.write({'cloudflare_status': 'failed'})
                    instance.add_to_log(f"[ERROR] Cloudflare integration error: {str(e)}")
