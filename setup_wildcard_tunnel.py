#!/usr/bin/env python3
"""
setup_wildcard_tunnel.py
────────────────────────
One-time script to configure a wildcard Cloudflare Tunnel route and DNS CNAME.
Run this ONCE to replace the per-instance Cloudflare API calls.

After running this successfully:
  1. All subdomains of aeisoftware.com automatically reach your K3s cluster.
  2. The portal's _configure_cloudflare() / _remove_cloudflare() are no longer needed.

Usage:
  export CF_API_TOKEN=...
  export CF_ACCOUNT_ID=...
  export CF_ZONE_ID=...
  export CF_TUNNEL_ID=...
  python3 setup_wildcard_tunnel.py

  # Dry-run (show what would change, make no changes):
  python3 setup_wildcard_tunnel.py --dry-run
"""

import os
import sys
import json
import argparse
import requests

TRAEFIK_SERVICE = "http://traefik.kube-system.svc.cluster.local:80"
WILDCARD_HOSTNAME = "*.aeisoftware.com"


def get_env():
    keys = ["CF_API_TOKEN", "CF_ACCOUNT_ID", "CF_ZONE_ID", "CF_TUNNEL_ID"]
    env = {k: os.environ.get(k) for k in keys}
    missing = [k for k, v in env.items() if not v]
    if missing:
        print(f"❌ Missing environment variables: {', '.join(missing)}")
        sys.exit(1)
    return env


def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Step 1: Wildcard DNS CNAME ─────────────────────────────────────────────────

def setup_wildcard_dns(env, dry_run=False):
    """Create (or confirm existence of) *.aeisoftware.com CNAME → tunnel."""
    h = headers(env["CF_API_TOKEN"])
    zone_id = env["CF_ZONE_ID"]
    tunnel_id = env["CF_TUNNEL_ID"]
    tunnel_target = f"{tunnel_id}.cfargotunnel.com"

    print("\n── Step 1: Wildcard DNS CNAME ──────────────────────────────")

    # Check if wildcard already exists
    r = requests.get(
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
        f"?type=CNAME&name=*.aeisoftware.com",
        headers=h, timeout=15
    )
    r.raise_for_status()
    existing = r.json().get("result", [])

    if existing:
        current_target = existing[0].get("content", "")
        if current_target == tunnel_target:
            print(f"✅ Wildcard CNAME already correct: *.aeisoftware.com → {tunnel_target}")
            return
        else:
            print(f"⚠️  Wildcard CNAME exists but points to: {current_target}")
            print(f"   Will update → {tunnel_target}")
            if not dry_run:
                rec_id = existing[0]["id"]
                upd = requests.put(
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{rec_id}",
                    headers=h, timeout=15,
                    json={"type": "CNAME", "name": "*.aeisoftware.com",
                          "content": tunnel_target, "proxied": True, "ttl": 1},
                )
                upd.raise_for_status()
                print(f"✅ Updated wildcard CNAME → {tunnel_target}")
            else:
                print("   [dry-run] Would update CNAME")
    else:
        print(f"   Creating *.aeisoftware.com CNAME → {tunnel_target}")
        if not dry_run:
            cr = requests.post(
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
                headers=h, timeout=15,
                json={"type": "CNAME", "name": "*.aeisoftware.com",
                      "content": tunnel_target, "proxied": True, "ttl": 1},
            )
            cr.raise_for_status()
            print(f"✅ Created wildcard CNAME → {tunnel_target}")
        else:
            print("   [dry-run] Would create wildcard CNAME")


# ── Step 2: Wildcard Tunnel Ingress Rule ───────────────────────────────────────

def setup_wildcard_tunnel_route(env, dry_run=False):
    """Replace all per-instance tunnel routes with a single wildcard rule."""
    h = headers(env["CF_API_TOKEN"])
    account_id = env["CF_ACCOUNT_ID"]
    tunnel_id = env["CF_TUNNEL_ID"]
    base = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations"

    print("\n── Step 2: Wildcard Tunnel Ingress Rule ────────────────────")

    r = requests.get(base, headers=h, timeout=15)
    r.raise_for_status()
    current_config = r.json().get("result", {}).get("config", {})
    current_ingress = current_config.get("ingress", [])

    print(f"   Current ingress rules ({len(current_ingress)}):")
    for rule in current_ingress:
        host = rule.get("hostname", "<catch-all>")
        svc  = rule.get("service", "")
        print(f"     {host:40s} → {svc}")

    # Check if wildcard is already there
    has_wildcard = any(r.get("hostname") == WILDCARD_HOSTNAME for r in current_ingress)
    if has_wildcard:
        print(f"\n✅ Wildcard rule already present: {WILDCARD_HOSTNAME} → {TRAEFIK_SERVICE}")
        print("   NOTE: You can still clean up per-instance rules if any remain.")

    # Count per-instance rules that will be replaced
    per_instance = [r for r in current_ingress
                    if r.get("hostname") and r.get("hostname") != WILDCARD_HOSTNAME]

    new_ingress = [
        {"hostname": WILDCARD_HOSTNAME, "service": TRAEFIK_SERVICE},
        {"service": "http_status:404"},  # required catch-all
    ]

    print(f"\n   New ingress ({len(new_ingress)} rules, replaces {len(per_instance)} per-instance routes):")
    for rule in new_ingress:
        host = rule.get("hostname", "<catch-all>")
        svc  = rule.get("service", "")
        print(f"     {host:40s} → {svc}")

    if not dry_run:
        confirm = input("\n⚠️  This will REPLACE all current tunnel ingress rules. Proceed? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            sys.exit(0)

        upd = requests.put(
            base, headers=h, timeout=15,
            json={"config": {"ingress": new_ingress}},
        )
        upd.raise_for_status()
        print(f"✅ Tunnel config updated (HTTP {upd.status_code})")
        print("   All traffic to *.aeisoftware.com now routes through the single wildcard rule.")
    else:
        print("\n   [dry-run] Would replace tunnel ingress with wildcard rule above")


def main():
    parser = argparse.ArgumentParser(description="Set up wildcard Cloudflare Tunnel route (run once)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without making changes")
    args = parser.parse_args()

    if args.dry_run:
        print("🔍 DRY RUN MODE — no changes will be made\n")

    env = get_env()
    print(f"Account : {env['CF_ACCOUNT_ID']}")
    print(f"Tunnel  : {env['CF_TUNNEL_ID']}")
    print(f"Zone    : {env['CF_ZONE_ID']}")

    setup_wildcard_dns(env, dry_run=args.dry_run)
    setup_wildcard_tunnel_route(env, dry_run=args.dry_run)

    print("\n🎉 Done.")
    if not args.dry_run:
        print("""
Next steps:
  1. Test: curl -I https://test.aeisoftware.com  (should reach Traefik, even if 404 from Odoo)
  2. In instances.py: remove calls to _configure_cloudflare() and _remove_cloudflare()
     (the portal no longer needs to manage Cloudflare per instance)
""")


if __name__ == "__main__":
    main()
