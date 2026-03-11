from odoo import models, fields

class SaasInstance(models.Model):
    _name = 'saas.instance'
    _description = 'SaaS Odoo Instance'
    _order = 'create_date desc'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char('Instance Name', required=True, tracking=True)
    domain = fields.Char('Domain', required=True, tracking=True)
    url = fields.Char('URL', compute='_compute_url', store=True)
    odoo_version = fields.Selection(
        [('17', 'Odoo 17'), ('18', 'Odoo 18'), ('19', 'Odoo 19')],
        string='Odoo Version', required=True, default='18',
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('provisioning', 'Provisioning'),
        ('running', 'Running'),
        ('error', 'Error'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True)

    partner_id = fields.Many2one('res.partner', string='Customer', required=True, tracking=True)
    sale_order_id = fields.Many2one('sale.order', string='Sale Order', ondelete='set null')
    product_id = fields.Many2one('product.product', string='Plan')

    db_template = fields.Char('DB Template', help='e.g. v18/starter.dump — path in Ceph RGW S3')
    addons_repo = fields.Char('Addons Git Repo', help='https://github.com/org/repo.git')
    custom_image = fields.Char('Custom Image', help='e.g. ghcr.io/org/odoo-custom:18')
    db_password = fields.Char('DB Password', default='odoo', groups='base.group_system')

    workers = fields.Integer('Workers', default=2)
    admin_passwd = fields.Char('Admin Password', groups='base.group_system')

    portal_response = fields.Text('Last Portal Response', readonly=True)
    error_message = fields.Text('Error', readonly=True)

    @fields.depends('domain')
    def _compute_url(self):
        for rec in self:
            rec.url = f"https://{rec.domain}" if rec.domain else ''

    def action_open_instance(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_url', 'url': self.url, 'target': 'new'}

    def action_provision(self):
        self.ensure_one()
        self._provision_via_portal()

    def action_cancel(self):
        self.ensure_one()
        self._deprovision_via_portal()
        self.state = 'cancelled'

    def _get_portal_config(self):
        """Return (portal_url, api_key) from ir.config_parameter."""
        get = self.env['ir.config_parameter'].sudo().get_param
        portal_url = get('saas.portal_url', 'https://portal.aeisoftware.com')
        api_key = get('saas.api_key', '')
        return portal_url, api_key

    def _provision_via_portal(self):
        import requests as req
        portal_url, api_key = self._get_portal_config()
        payload = {
            'name': self.name,
            'domain': self.domain,
            'odoo_version': self.odoo_version,
            'db_password': self.db_password or 'odoo',
            'db_template': self.db_template or None,
            'addons_repo': self.addons_repo or None,
            'image': self.custom_image or None,
            'odoo_conf_overrides': {
                'workers': self.workers,
                **(({'admin_passwd': self.admin_passwd}) if self.admin_passwd else {}),
            },
        }
        try:
            r = req.post(
                f"{portal_url}/api/instances",
                json=payload,
                headers={'X-API-Key': api_key},
                timeout=30,
            )
            r.raise_for_status()
            self.write({
                'state': 'provisioning',
                'portal_response': r.text,
                'error_message': False,
            })
            self.message_post(body=f"✅ Instance provisioning started. URL: {self.url}")
            # Send "ready" email to customer
            template = self.env.ref('odoo_k8s_saas.mail_template_instance_ready', raise_if_not_found=False)
            if template:
                template.send_mail(self.id, force_send=True)
        except Exception as e:
            self.write({'state': 'error', 'error_message': str(e)})
            self.message_post(body=f"❌ Provisioning failed: {e}")

    def _deprovision_via_portal(self):
        import requests as req
        portal_url, api_key = self._get_portal_config()
        try:
            req.delete(
                f"{portal_url}/api/instances/{self.name}",
                params={'domain': self.domain},
                headers={'X-API-Key': api_key},
                timeout=30,
            )
            self.message_post(body="🗑 Instance deleted from K3s.")
        except Exception as e:
            self.message_post(body=f"⚠ Delete request failed: {e}")
