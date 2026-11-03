from django.apps import AppConfig


class VouchersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vouchers"

    def ready(self):
        from . import signals  # noqa: F401
