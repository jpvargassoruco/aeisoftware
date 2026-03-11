from odoo import models, fields


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    saas_portal_url = fields.Char(
        string='SaaS Portal URL',
        config_parameter='saas.portal_url',
        default='https://portal.aeisoftware.com',
    )
    saas_api_key = fields.Char(
        string='Portal API Key',
        config_parameter='saas.api_key',
    )
