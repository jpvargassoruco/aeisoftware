import os
import sys
import argparse
import logging
from cloudflare_manager.client import CloudflareTunnelManager

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser(description="Cloudflare Provisioning Wrapper")
    parser.add_argument("--hostname", required=True, help="The full domain name (e.g., client1.aeisoftware.com)")
    parser.add_argument("--service-url", required=True, help="The local service URL (e.g., http://localhost:80)")
    parser.add_argument("--cname-name", required=True, help="The CNAME record name (e.g., client1)")
    
    args = parser.parse_args()

    # Get environment variables
    api_token = os.environ.get("CF_API_TOKEN")
    account_id = os.environ.get("CF_ACCOUNT_ID")
    zone_id = os.environ.get("CF_ZONE_ID")
    tunnel_id = os.environ.get("CF_TUNNEL_ID")

    if not all([api_token, account_id, zone_id, tunnel_id]):
        logger.error("Missing required environment variables: CF_API_TOKEN, CF_ACCOUNT_ID, CF_ZONE_ID, CF_TUNNEL_ID")
        sys.exit(1)

    # Initialize manager
    manager = CloudflareTunnelManager(api_token, account_id, zone_id)

    # 1. Add route to tunnel
    logger.info(f"Adding route for {args.hostname} -> {args.service_url}")
    if not manager.add_route_to_tunnel(tunnel_id, args.hostname, args.service_url):
        logger.error("Failed to add route to tunnel")
        sys.exit(1)

    # 2. Create DNS CNAME
    logger.info(f"Creating DNS CNAME record for {args.cname_name}")
    if not manager.create_dns_cname(args.cname_name, tunnel_id):
        logger.error("Failed to create DNS CNAME record")
        sys.exit(1)

    logger.info("Cloudflare provisioning completed successfully.")

if __name__ == "__main__":
    main()
