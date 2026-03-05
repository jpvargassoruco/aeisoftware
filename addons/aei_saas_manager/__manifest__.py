# -*- coding: utf-8 -*-
{
    "name": "AEI SaaS Manager",
    "summary": "Automates Cloudflare Tunnel routing for Odoo Micro SaaS instances",
    "description": """
        Extends the odoo_micro_saas module to automatically publish the newly
        created Odoo containers to a Cloudflare tunnel using the Cloudflare API.
    """,
    "author": "AEI Software",
    "website": "https://aeisoftware.com",
    "category": "Tools",
    "version": "1.0.0",
    "depends": ["base", "micro_saas"],
    "data": [
        "views/odoo_docker_instance_inherit.xml",
    ],
    "installable": True,
    "application": False,
    "auto_install": False,
}
