import requests
import logging

logger = logging.getLogger(__name__)

class CloudflareTunnelManager:
    """
    A standalone client to manage Cloudflare Tunnels and DNS records.
    """
    def __init__(self, api_token: str, account_id: str, zone_id: str):
        self.api_token = api_token
        self.account_id = account_id
        self.zone_id = zone_id
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }

    def get_tunnel_config(self, tunnel_id: str) -> dict:
        """
        Retrieve the current ingress configuration for a tunnel.
        """
        url = f"{self.base_url}/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/configurations"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        data = response.json()
        return data.get('result') or {}

    def update_tunnel_config(self, tunnel_id: str, new_config: dict) -> dict:
        """
        Overwrites the tunnel configuration with the new_config provided.
        """
        url = f"{self.base_url}/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/configurations"
        response = requests.put(url, headers=self.headers, json=new_config)
        response.raise_for_status()
        data = response.json()
        return data.get('result') or {}

    def add_route_to_tunnel(self, tunnel_id: str, hostname: str, service: str) -> bool:
        """
        Safely adds a new route to an existing tunnel.
        It fetches current config, appends the new route BEFORE the catch-all, and updates.
        """
        try:
            current_data = self.get_tunnel_config(tunnel_id)
            if not current_data:
                logger.error(f"Could not fetch data for tunnel {tunnel_id}")
                return False

            # The config object is usually nested like: {"config": {"ingress": [...]}}
            config = current_data.get('config') or {}
            ingress_rules = config.get('ingress', [])
            
            new_rule = {
                "hostname": hostname,
                "service": service
            }

            if not ingress_rules:
                logger.info("No existing ingress rules found. Initializing.")
                # If empty, create with the new rule and a default catch-all
                ingress_rules = [
                    new_rule,
                    {"service": "http_status:404"}
                ]
            else:
                # Check if route already exists
                for rule in ingress_rules:
                    if rule.get('hostname') == hostname:
                        logger.info(f"Hostname {hostname} already exists in tunnel {tunnel_id}.")
                        return True

                # Insert before the last catch-all rule (which usually has no hostname and service http_status:404)
                catch_all_idx = len(ingress_rules)
                for i, rule in enumerate(ingress_rules):
                    if 'hostname' not in rule and 'http_status:404' in rule.get('service', ''):
                        catch_all_idx = i
                        break
                
                ingress_rules.insert(catch_all_idx, new_rule)
            
            # Prepare payload
            payload = {"config": {"ingress": ingress_rules}}
            self.update_tunnel_config(tunnel_id, payload)
            logger.info(f"Successfully added {hostname} routing to {service}.")
            return True

        except Exception as e:
            logger.error(f"Failed to add route to tunnel: {e}")
            return False

    def create_dns_cname(self, name: str, tunnel_id: str) -> bool:
        """
        Creates a DNS CNAME record pointing to the tunnel.
        name: e.g. 'test' (which will become test.yourdomain.com)
        """
        try:
            url = f"{self.base_url}/zones/{self.zone_id}/dns_records"
            payload = {
                "type": "CNAME",
                "name": name,
                "content": f"{tunnel_id}.cfargotunnel.com",
                "proxied": True
            }
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            logger.info(f"Successfully created CNAME record for {name}.")
            return True
        except requests.exceptions.HTTPError as e:
            # Check if it already exists (Cloudflare throws 400 with code 81053)
            error_data = e.response.json()
            errors = error_data.get('errors', [])
            if any(err.get('code') == 81053 for err in errors):
                logger.info(f"CNAME record for {name} already exists.")
                return True
            logger.error(f"Failed to create DNS record: {e} - {error_data}")
            return False
        except Exception as e:
            logger.error(f"Failed to create DNS record: {e}")
            return False
