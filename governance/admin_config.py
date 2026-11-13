from django.contrib.admin.apps import AdminConfig


class MfaAdminConfig(AdminConfig):
    default_site = "governance.admin_site.MfaAdminSite"
