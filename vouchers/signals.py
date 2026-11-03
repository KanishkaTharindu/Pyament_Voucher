from django.contrib.auth.models import Group
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import post_save, post_migrate
from django.dispatch import receiver

from .models import Branch, UserProfile, Voucher
from .services import ROLE_GROUPS

User = get_user_model()


@receiver(post_migrate)
def create_voucher_roles(sender, **kwargs):
    if sender.name != "vouchers":
        return
    voucher_type = ContentType.objects.get_for_model(Voucher)
    branch_type = ContentType.objects.get_for_model(Branch)
    for group_name in ROLE_GROUPS:
        group, _ = Group.objects.get_or_create(name=group_name)
        if group_name == "Voucher Approvers":
            permission = Permission.objects.filter(
                content_type=voucher_type, codename="approve_voucher"
            ).first()
            if permission:
                group.permissions.add(permission)
        elif group_name == "Finance Admins":
            permissions = Permission.objects.filter(
                content_type=voucher_type,
                codename__in=["approve_voucher", "approve_any_voucher"],
            )
            permissions |= Permission.objects.filter(
                content_type=branch_type,
                codename__in=[
                    "view_all_branches",
                    "view_finance_reports",
                    "manage_branches",
                ],
            )
            group.permissions.add(*permissions)


@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)
