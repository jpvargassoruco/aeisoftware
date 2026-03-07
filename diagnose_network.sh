#!/bin/bash
echo "--- Docker Network Status ---"
sudo docker network ls
echo ""
echo "--- Inspecting web-proxy ---"
sudo docker network inspect web-proxy
echo ""
echo "--- Container Status ---"
sudo docker ps
echo ""
echo "--- Testing connectivity from Cloudflare Tunnel to Traefik ---"
sudo docker exec cloudflare-tunnel ping -c 3 traefik
sudo docker exec cloudflare-tunnel curl -I http://traefik:80
echo ""
echo "--- Cloudflare Tunnel Logs ---"
sudo docker logs cloudflare-tunnel --tail 20
