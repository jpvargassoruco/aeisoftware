from odoo import models, fields, api
import re


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    is_saas_plan = fields.Boolean('Is SaaS Plan', default=False)
    saas_odoo_version = fields.Selection(
        [('17', 'Odoo 17'), ('18', 'Odoo 18'), ('19', 'Odoo 19')],
        string='Odoo Version', default='18',
    )
    saas_db_template = fields.Char(
        'DB Template Key',
        help='Path in Ceph RGW bucket, e.g. v18/starter.dump',
    )
    saas_addons_repo = fields.Char(
        'Addons Git Repo',
        help='https://github.com/org/client-addons.git',
    )
    saas_custom_image = fields.Char(
        'Custom Docker Image',
        help='e.g. ghcr.io/jpvargassoruco/aeisoftware/odoo-custom:18',
    )
    saas_workers = fields.Integer('Workers', default=2)
    saas_domain_suffix = fields.Char(
        'Domain Suffix',
        default='.aeisoftware.com',
        help='Will be appended to the instance slug, e.g. .aeisoftware.com',
    )


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    saas_instance_id = fields.Many2one('saas.instance', string='Created Instance', readonly=True)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    saas_instance_ids = fields.One2many(
        'saas.instance', 'sale_order_id', string='SaaS Instances',
    )
    saas_instance_count = fields.Integer(
        compute='_compute_saas_instance_count', string='Instances',
    )

    @api.depends('saas_instance_ids')
    def _compute_saas_instance_count(self):
        for order in self:
            order.saas_instance_count = len(order.saas_instance_ids)

    def action_view_saas_instances(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'SaaS Instances',
            'res_model': 'saas.instance',
            'view_mode': 'list,form',
            'domain': [('sale_order_id', '=', self.id)],
            'context': {'default_sale_order_id': self.id},
        }

    def action_confirm(self):
        res = super().action_confirm()
        for order in self:
            for line in order.order_line:
                product = line.product_id.product_tmpl_id
                if product.is_saas_plan:
                    order._provision_saas_instance(line)
        return res

    def _provision_saas_instance(self, line):
        """Create and provision a SaaS instance from a sale order line."""
        product = line.product_id.product_tmpl_id
        partner = self.partner_id

        # Build instance slug from partner name: lowercase alphanum + hyphens
        slug = re.sub(r'[^a-z0-9]', '-', partner.name.lower())[:24].strip('-')
        slug = re.sub(r'-+', '-', slug)
        domain = f"{slug}{product.saas_domain_suffix or '.aeisoftware.com'}"

        # Ensure uniqueness - append order id if slug already exists
        existing = self.env['saas.instance'].search([('name', '=', slug)])
        if existing:
            slug = f"{slug}-{self.id}"
            domain = f"{slug}{product.saas_domain_suffix or '.aeisoftware.com'}"

        instance = self.env['saas.instance'].create({
            'name': slug,
            'domain': domain,
            'odoo_version': product.saas_odoo_version or '18',
            'partner_id': partner.id,
            'sale_order_id': self.id,
            'product_id': line.product_id.id,
            'db_template': product.saas_db_template,
            'addons_repo': product.saas_addons_repo,
            'custom_image': product.saas_custom_image,
            'workers': product.saas_workers or 2,
        })
        line.saas_instance_id = instance
        instance.action_provision()
        return instance
