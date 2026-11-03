import logging
from datetime import date, timedelta
from decimal import Decimal
from smtplib import SMTPException

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .models import (
    AuditLog,
    CompanySettings,
    ExpenseCategory,
    RecurringVoucher,
    UserProfile,
    Voucher,
)

logger = logging.getLogger(__name__)
User = get_user_model()

ROLE_FOR_STATUS = {
    Voucher.Status.PREPARED: ("Voucher Recommenders", Voucher.Status.RECOMMENDED),
    Voucher.Status.RECOMMENDED: ("Voucher Approvers", Voucher.Status.APPROVED),
    Voucher.Status.APPROVED: ("Voucher Authorizers", Voucher.Status.AUTHORIZED),
}

ROLE_FOR_NEXT_STATUS = {
    Voucher.Status.PREPARED: "Voucher Recommenders",
    Voucher.Status.RECOMMENDED: "Voucher Approvers",
    Voucher.Status.APPROVED: "Voucher Authorizers",
}

ROLE_GROUPS = (
    "Voucher Preparers",
    "Voucher Recommenders",
    "Voucher Approvers",
    "Voucher Authorizers",
    "Finance Admins",
)


class WorkflowError(Exception):
    pass


class NotificationDeliveryError(WorkflowError):
    pass


def user_in_role(user, role):
    return user.is_superuser or user.groups.filter(name=role).exists()


def user_branch(user):
    if not user.is_authenticated:
        return None
    try:
        return user.voucher_profile.branch
    except UserProfile.DoesNotExist:
        return None


def branch_features_enabled():
    return CompanySettings.load().branches_enabled


def _permission(app_label, codename):
    return Permission.objects.filter(
        content_type__app_label=app_label, codename=codename
    ).first()


def can_view_all_branches(user):
    if not branch_features_enabled():
        return True
    return (
        user.is_superuser
        or user_in_role(user, "Finance Admins")
        or user.has_perm("vouchers.view_all_branches")
    )


def can_manage_branches(user):
    return (
        user.is_superuser
        or user_in_role(user, "Finance Admins")
        or user.has_perm("vouchers.manage_branches")
    )


def branch_scope(queryset, user, field_name="branch"):
    if not branch_features_enabled() or can_view_all_branches(user):
        return queryset
    branch = user_branch(user)
    if branch:
        return queryset.filter(**{f"{field_name}_id": branch.pk})
    return queryset.filter(**{f"{field_name}__isnull": True})


def eligible_approvers(branch=None):
    approval_permission = _permission("vouchers", "approve_voucher")
    if approval_permission is None:
        return User.objects.none()
    users = User.objects.filter(is_active=True).filter(
        Q(is_superuser=True)
        | Q(groups__permissions=approval_permission)
        | Q(user_permissions=approval_permission)
    )
    if branch:
        cross_branch_permission = _permission("vouchers", "view_all_branches")
        cross_branch_filter = (
            Q(voucher_profile__branch__isnull=True)
            | Q(groups__name="Finance Admins")
            | Q(is_superuser=True)
        )
        if cross_branch_permission:
            cross_branch_filter |= Q(
                groups__permissions=cross_branch_permission
            ) | Q(user_permissions=cross_branch_permission)
        users = users.filter(
            Q(voucher_profile__branch_id=branch.pk) | cross_branch_filter
        )
    return users.distinct().order_by("last_name", "first_name", "username")


def can_approve_any(user):
    return (
        user.is_authenticated
        and user.is_active
        and (
            user.is_superuser
            or user_in_role(user, "Finance Admins")
            or user.has_perm("vouchers.approve_any_voucher")
        )
    )


def can_approve_assigned(user, voucher):
    if not user.is_authenticated or not user.is_active:
        return False
    if can_approve_any(user):
        return True
    if voucher.approver_id != user.pk or not user.has_perm("vouchers.approve_voucher"):
        return False
    if branch_features_enabled() and voucher.branch_id:
        branch = user_branch(user)
        return branch is None or branch.pk == voucher.branch_id or user.has_perm(
            "vouchers.view_all_branches"
        )
    return True


def can_view_voucher(user, voucher):
    if not user.is_authenticated or not user.is_active:
        return False
    if branch_features_enabled() and not can_view_all_branches(user):
        branch = user_branch(user)
        if voucher.branch_id != (branch.pk if branch else None):
            return False
    if user_in_role(user, "Finance Admins") or voucher.prepared_by_id == user.pk:
        return True
    if voucher.status == Voucher.Status.PREPARED and can_approve_any(user):
        return True
    if voucher.audit_logs.filter(actor=user).exists():
        return True
    if voucher.status == Voucher.Status.PREPARED and voucher.approver_id:
        return can_approve_assigned(user, voucher)
    required_role = ROLE_FOR_NEXT_STATUS.get(voucher.status)
    return bool(required_role and user_in_role(user, required_role))


def can_manage_vouchers(user):
    return (
        user.is_superuser
        or user.has_perm("vouchers.add_voucher")
        or user.groups.filter(
            name__in=["Voucher Preparers", "Finance Admins"]
        ).exists()
    )


def can_manage_recurring(user):
    return can_manage_vouchers(user)


def record_audit(voucher, actor, action, previous_status="", notes="", ip_address=None):
    return AuditLog.objects.create(
        voucher=voucher,
        actor=actor,
        action=action,
        previous_status=previous_status,
        new_status=voucher.status,
        notes=notes,
        ip_address=ip_address,
    )


def _check_separation_of_duties(voucher, actor, allow_self_approval=False):
    if voucher.prepared_by_id == actor.pk and not (
        allow_self_approval
        and actor.is_authenticated
        and actor.is_active
        and actor.has_perm("vouchers.approve_voucher")
    ):
        raise WorkflowError("The preparer cannot approve or reject their own voucher.")
    if actor.pk in {voucher.recommended_by_id, voucher.approved_by_id}:
        raise WorkflowError("A different person must complete each approval stage.")


def _check_budget(voucher):
    category = ExpenseCategory.objects.select_for_update().get(pk=voucher.category_id)
    if category.monthly_budget_limit <= Decimal("0.00"):
        return
    month_start = date(voucher.date.year, voucher.date.month, 1)
    month_end = date(
        voucher.date.year + (voucher.date.month == 12),
        1 if voucher.date.month == 12 else voucher.date.month + 1,
        1,
    )
    already_spent = (
        Voucher.objects.filter(
            category=category,
            status__in=[
                Voucher.Status.APPROVED,
                Voucher.Status.AUTHORIZED,
                Voucher.Status.NO_APPROVAL,
            ],
            date__gte=month_start,
            date__lt=month_end,
        )
        .exclude(pk=voucher.pk)
        .aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )
    if already_spent + voucher.amount > category.monthly_budget_limit:
        remaining = max(category.monthly_budget_limit - already_spent, Decimal("0.00"))
        raise WorkflowError(
            f"Submitting this voucher would exceed the monthly budget for "
            f"{category.name}. Remaining budget: {remaining:.2f}."
        )


def _send_notification(voucher, action):
    if action == "submit":
        recipients = [voucher.approver.email] if voucher.approver and voucher.approver.email else []
        subject = f"Voucher {voucher.ref_no} requires your approval"
        message = (
            f"Voucher {voucher.ref_no} for {voucher.amount:.2f} "
            f"was submitted by {voucher.prepared_by.get_username()} and is awaiting your approval."
        )
    else:
        recipients = [voucher.prepared_by.email] if voucher.prepared_by.email else []
        outcome = "approved" if action == "approve" else "rejected"
        subject = f"Voucher {voucher.ref_no} was {outcome}"
        message = (
            f"Voucher {voucher.ref_no} for {voucher.amount:.2f} "
            f"is now {voucher.get_status_display()}."
        )
        if action == "reject":
            message += f"\nReason: {voucher.rejection_reason}"
    if recipients:
        try:
            send_mail(subject, message, None, recipients, fail_silently=False)
        except (SMTPException, OSError) as exc:
            logger.exception(
                "Could not send %s notification for voucher %s", action, voucher.ref_no
            )
            raise NotificationDeliveryError(
                f"Voucher status was updated, but email notification could not be sent: {exc}"
            ) from exc


def _notify_legacy_role(voucher, status):
    group_name = ROLE_FOR_NEXT_STATUS.get(status)
    if not group_name:
        return
    recipients = list(
        Group.objects.filter(name=group_name)
        .values_list("user__email", flat=True)
        .exclude(user__email="")
        .distinct()
    )
    if recipients:
        try:
            send_mail(
                subject=f"Voucher {voucher.ref_no} requires attention",
                message=(
                    f"Voucher {voucher.ref_no} for {voucher.amount:.2f} "
                    f"is now {voucher.get_status_display()} and is awaiting your review."
                ),
                from_email=None,
                recipient_list=recipients,
                fail_silently=False,
            )
        except (SMTPException, OSError) as exc:
            logger.exception("Could not send workflow notification for %s", voucher.ref_no)
            raise NotificationDeliveryError(
                f"Voucher status was updated, but email notification could not be sent: {exc}"
            ) from exc


def transition_voucher(
    voucher_id,
    actor,
    action,
    notes="",
    ip_address=None,
    authorized_signature=None,
):
    legacy_notification = False
    with transaction.atomic():
        voucher = (
            Voucher.objects.select_for_update()
            .select_related("category", "approver", "prepared_by")
            .get(pk=voucher_id)
        )
        previous_status = voucher.status
        if action in {"submit", "prepare"}:
            if voucher.status != Voucher.Status.DRAFT:
                raise WorkflowError("Only a draft voucher can be submitted.")
            if voucher.prepared_by_id != actor.pk:
                raise WorkflowError("Only the voucher creator can submit it.")
            if branch_features_enabled() and not voucher.branch_id:
                raise WorkflowError(
                    "Assign this voucher to an active branch before submitting it."
                )
            if voucher.requires_approval:
                if not voucher.approver_id:
                    raise WorkflowError("Choose an approver before submitting this voucher.")
                if not voucher.approver.is_active or not eligible_approvers(
                    voucher.branch
                ).filter(pk=voucher.approver_id).exists():
                    raise WorkflowError(
                        "The selected approver is no longer active or authorized."
                    )
                voucher.status = Voucher.Status.PREPARED
                voucher.approval_requested_at = timezone.now()
                record_action = "submitted"
            else:
                _check_budget(voucher)
                voucher.status = Voucher.Status.NO_APPROVAL
                voucher.approver = None
                voucher.approval_requested_at = None
                record_action = "created_without_approval"
            voucher.save()
            record_audit(
                voucher,
                actor,
                record_action,
                previous_status=previous_status,
                ip_address=ip_address,
            )
            action = "submit" if voucher.status == Voucher.Status.PREPARED else ""
        elif action == "reject":
            if not notes.strip():
                raise WorkflowError("Enter a reason before rejecting a voucher.")
            if voucher.approver_id:
                if (
                    voucher.status != Voucher.Status.PREPARED
                    or not can_approve_assigned(actor, voucher)
                ):
                    raise WorkflowError(
                        "Only the selected approver or an authorized administrator can reject this voucher."
                    )
            else:
                stage = ROLE_FOR_STATUS.get(voucher.status)
                if not stage or not (
                    user_in_role(actor, stage[0]) or can_approve_any(actor)
                ):
                    raise WorkflowError(
                        "You are not allowed to reject this voucher at its current stage."
                    )
            _check_separation_of_duties(voucher, actor)
            voucher.status = Voucher.Status.REJECTED
            voucher.rejection_reason = notes.strip()
            voucher.rejected_at = timezone.now()
            voucher.save()
            record_audit(
                voucher,
                actor,
                "rejected",
                previous_status=previous_status,
                notes=voucher.rejection_reason,
                ip_address=ip_address,
            )
        elif action == "approve":
            if voucher.approver_id:
                if (
                    voucher.status != Voucher.Status.PREPARED
                    or not can_approve_assigned(actor, voucher)
                ):
                    raise WorkflowError(
                        "Only the selected approver or an authorized administrator can approve this voucher."
                    )
                _check_separation_of_duties(voucher, actor, allow_self_approval=True)
                _check_budget(voucher)
                voucher.approved_by = actor
                voucher.approved_at = timezone.now()
                voucher.authorized_signature = authorized_signature or voucher.authorized_signature
                voucher.status = Voucher.Status.APPROVED
            else:
                stage = ROLE_FOR_STATUS.get(voucher.status)
                if not stage:
                    raise WorkflowError("This voucher is not awaiting an approval action.")
                role, next_status = stage
                if not (user_in_role(actor, role) or can_approve_any(actor)):
                    raise WorkflowError(f"You need the {role} role to complete this step.")
                _check_separation_of_duties(voucher, actor)
                if next_status == Voucher.Status.AUTHORIZED:
                    _check_budget(voucher)
                    voucher.authorized_by = actor
                    voucher.approved_at = voucher.approved_at or timezone.now()
                    if authorized_signature:
                        voucher.authorized_signature = authorized_signature
                elif next_status == Voucher.Status.APPROVED:
                    voucher.approved_by = actor
                    voucher.approved_at = timezone.now()
                else:
                    voucher.recommended_by = actor
                voucher.status = next_status
                legacy_notification = True
            voucher.save()
            record_audit(
                voucher,
                actor,
                "approved",
                previous_status=previous_status,
                ip_address=ip_address,
            )
        else:
            raise WorkflowError("Unknown workflow action.")

    if action == "submit":
        _send_notification(voucher, "submit")
    elif legacy_notification:
        _notify_legacy_role(voucher, voucher.status)
    elif action in {"reject", "approve"}:
        _send_notification(voucher, action)
    return voucher


def advance_date(current, frequency):
    if frequency == "weekly":
        return current + timedelta(days=7)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[frequency]
    month_index = current.month - 1 + months
    year = current.year + month_index // 12
    month = month_index % 12 + 1
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(current.day, last_day))


def generate_due_recurring():
    """Create all missed recurring vouchers and advance their schedules."""
    today = timezone.localdate()
    generated = 0
    due_ids = RecurringVoucher.objects.filter(
        is_active=True, next_run__lte=today
    ).values_list("pk", flat=True)
    for recurring_id in due_ids:
        with transaction.atomic():
            recurring = (
                RecurringVoucher.objects.select_for_update()
                .select_related("category", "prepared_by", "approver", "branch")
                .get(pk=recurring_id)
            )
            while recurring.is_active and recurring.next_run <= today:
                due_date = recurring.next_run
                voucher, created = Voucher.objects.get_or_create(
                    recurring_template=recurring,
                    recurring_period_date=due_date,
                    defaults={
                        "amount": recurring.amount,
                        "date": due_date,
                        "cash": recurring.cash,
                        "method_of_payment": recurring.method_of_payment,
                        "to": recurring.to,
                        "account_details": recurring.account_details,
                        "being": recurring.being,
                        "payee": recurring.payee,
                        "category": recurring.category,
                        "branch": recurring.branch,
                        "requires_approval": recurring.requires_approval,
                        "approver": recurring.approver,
                        "prepared_by": recurring.prepared_by,
                    },
                )
                if created:
                    AuditLog.objects.create(
                        voucher=voucher,
                        actor=recurring.prepared_by,
                        action="generated",
                        new_status=voucher.status,
                        notes=f"Generated from recurring schedule: {recurring.title}",
                    )
                    generated += 1
                recurring.next_run = advance_date(due_date, recurring.frequency)
            recurring.save(update_fields=["next_run"])
    return generated
