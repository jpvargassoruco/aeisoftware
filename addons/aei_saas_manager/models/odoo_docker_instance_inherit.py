import logging
import os
import re
from datetime import datetime
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
            
            # Sanitize the name: replace ANY non-alphanumeric character with underscore
            # This prevents shell syntax errors from parentheses, spaces, dots, etc.
            safe_name = re.sub(r'[^a-zA-Z0-9\-_]', '_', instance.name.lower())
            # Also collapse multiple underscores
            safe_name = re.sub(r'_+', '_', safe_name).strip('_')
            
            instance.instance_data_path = os.path.join(instance.user_path, 'odoo_docker', 'data', safe_name)
            
            # Ensure variables are synced from template if it's a new instance
            if instance.template_id and not instance.variable_ids:
                instance.variable_ids = instance.template_id.variable_ids

            # Regenerate result_dc_body to ensure any path-dependent variables are updated
            # Use demo_fallback=True to use the values assigned to the instance's variable_ids
            instance.result_dc_body = instance._get_formatted_body(template_body=instance.template_dc_body,
                                                                   demo_fallback=True)

    def add_to_log(self, message):
        """Override to handle False initially and keep formatting clean"""
        now = datetime.now()
        timestamp = now.strftime("%m/%d/%Y, %H:%M:%S")
        prefix = f"</br> \n#{timestamp} "
        
        current_log = self.log if self.log else ""
        new_log = f"{prefix}{message} {current_log}"
        
        if len(new_log) > 10000:
            new_log = f"{prefix}{message}"
        self.log = new_log

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

    def _create_odoo_conf(self):
        """Override to ensure permissions and required settings"""
        super(OdooDockerInstanceCF, self)._create_odoo_conf()
        for instance in self:
            # Fix permissions for the data directory so Odoo container (uid 101) can write to it
            try:
                # We ensure the entire instance path is writable
                cmd = f'sudo chown -R 101:101 "{instance.instance_data_path}"'
                # Note: Master Odoo must have sudo permission or we used a shell command.
                # Since we are inside the container, we might need a different approach if sudo is not there.
                # However, the Master runs as root, so we can use os.chown if we traverse or just call a command.
                instance.excute_command(cmd, shell=True) 
            except Exception as e:
                instance.add_to_log(f"[WARNING] Failed to set directory permissions: {str(e)}")

    def _create_nginx_conf(self):
        for instance in self:
            nginx_conf_path = os.path.join(instance.instance_data_path, 'nginx.conf')
            nginx_conf_content = """user  nginx;
worker_processes  auto;
error_log  /var/log/nginx/error.log notice;
pid        /var/run/nginx.pid;

events {
    worker_connections 1024;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    log_format  main  '$remote_addr - $remote_user [$time_local] "$request" '
                      '$status $body_bytes_sent "$http_referer" '
                      '"$http_user_agent" "$http_x_forwarded_for"';

    access_log  /var/log/nginx/access.log  main;

    sendfile        on;
    keepalive_timeout  65;

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
