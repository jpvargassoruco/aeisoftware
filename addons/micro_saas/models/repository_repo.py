from odoo import models, fields


class RepositoryRepo(models.Model):
    _name = 'repository.repo'
    _description = 'Repository and Branch'

    name = fields.Char(string='Repository Name')


class RepositoryRepoLine(models.Model):
    _name = 'repository.repo.line'
    _description = 'Repository and Branch'

    name = fields.Char(string='Branch Name')
    repository_id = fields.Many2one('repository.repo', string='Repository')
    instance_id = fields.Many2one('odoo.docker.instance', string='Instance')
    dc_template_id = fields.Many2one('docker.compose.template', string='Template')
    is_clone = fields.Boolean(string='Is Clone')
