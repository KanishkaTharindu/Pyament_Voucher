import logging
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Prefetch, Q, Sum
from django.http import FileResponse, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .forms import (
    AuthorizationForm,
    BranchForm,
    CompanySettingsForm,
    GroupManagementForm,
    RecurringVoucherForm,
    RejectionForm,
    UserManagementForm,
    VoucherFilterForm,
    VoucherForm,
)
from .models import (
    AuditLog,
    Branch,
    CompanySettings,
    ExpenseCategory,
    RecurringVoucher,
    Voucher,
)
from .services import (
    WorkflowError,
    NotificationDeliveryError,
    ROLE_GROUPS,
    branch_scope,
    branch_features_enabled,
    can_approve_any,
    can_approve_assigned,
    can_manage_branches,
    can_view_all_branches,
    can_manage_recurring,
    can_manage_vouchers,
    can_view_voucher,
    eligible_approvers,
    record_audit,
    transition_voucher,
    user_in_role,
    user_branch,
)

logger = logging.getLogger(__name__)
User = get_user_model()


def _client_ip(request):
    return request.META.get("REMOTE_ADDR")


def _role_statuses(user):
    statuses = []
    if user_in_role(user, "Voucher Recommenders"):
        statuses.append(Voucher.Status.PREPARED)
    if user_in_role(user, "Voucher Approvers"):
        statuses.append(Voucher.Status.RECOMMENDED)
    if user_in_role(user, "Voucher Authorizers"):
        statuses.append(Voucher.Status.APPROVED)
    return statuses


def _pending_for_user(user):
    queryset = branch_scope(
        Voucher.objects.select_related("category", "prepared_by", "approver"),
        user,
    )
    if can_approve_any(user):
        return queryset.filter(
            status__in=[
                Voucher.Status.PREPARED,
                Voucher.Status.RECOMMENDED,
                Voucher.Status.APPROVED,
            ]
        ).exclude(
            Q(approver__isnull=False)
            & Q(status__in=[Voucher.Status.APPROVED, Voucher.Status.AUTHORIZED])
        )
    assigned = Q(status=Voucher.Status.PREPARED, approver=user)
    legacy_statuses = _role_statuses(user)
    legacy = Q(status__in=legacy_statuses, approver__isnull=True)
    return queryset.filter(assigned | legacy).distinct()


def _can_act_on_voucher(user, voucher):
    if voucher.status == Voucher.Status.PREPARED and voucher.approver_id:
        return (
            can_approve_assigned(user, voucher)
            and voucher.prepared_by_id != user.pk
        )
    if (
        can_approve_any(user)
        and voucher.status in {
            Voucher.Status.PREPARED,
            Voucher.Status.RECOMMENDED,
            Voucher.Status.APPROVED,
        }
        and voucher.approver_id is None
        and voucher.prepared_by_id != user.pk
        and user.pk not in {voucher.recommended_by_id, voucher.approved_by_id}
    ):
        return True
    stage_role = {
        Voucher.Status.PREPARED: "Voucher Recommenders",
        Voucher.Status.RECOMMENDED: "Voucher Approvers",
        Voucher.Status.APPROVED: "Voucher Authorizers",
    }.get(voucher.status)
    return bool(
        stage_role
        and voucher.approver_id is None
        and user_in_role(user, stage_role)
        and voucher.prepared_by_id != user.pk
        and user.pk not in {voucher.recommended_by_id, voucher.approved_by_id}
    )


@login_required
def dashboard(request):
    today = timezone.localdate()
    month_start = today.replace(day=1)
    queryset = branch_scope(
        Voucher.objects.select_related("category", "prepared_by"), request.user
    )
    if not can_approve_any(request.user):
        queryset = queryset.filter(
            Q(prepared_by=request.user)
            | Q(status=Voucher.Status.PREPARED, approver=request.user)
            | (Q(status__in=_role_statuses(request.user), approver__isnull=True))
            | Q(audit_logs__actor=request.user)
        ).distinct()
    metrics = {
        "total": queryset.count(),
        "pending": queryset.filter(
            status__in=[
                Voucher.Status.PREPARED,
                Voucher.Status.RECOMMENDED,
                Voucher.Status.APPROVED,
            ]
        ).count(),
        "authorized": queryset.filter(
            status__in=[
                Voucher.Status.APPROVED,
                Voucher.Status.AUTHORIZED,
                Voucher.Status.NO_APPROVAL,
            ]
        ).count(),
        "month_total": queryset.filter(
            date__gte=month_start, date__lte=today
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00"),
    }
    recent_vouchers = queryset.order_by("-created_at")[:8]
    category_spend = (
        queryset.filter(
            status__in=[
                Voucher.Status.APPROVED,
                Voucher.Status.AUTHORIZED,
                Voucher.Status.NO_APPROVAL,
            ],
            date__gte=month_start,
            date__lte=today,
        )
        .values("category__name")
        .annotate(total=Sum("amount"))
        .order_by("-total")[:6]
    )
    return render(
        request,
        "vouchers/dashboard.html",
        {
            "metrics": metrics,
            "recent_vouchers": recent_vouchers,
            "category_spend": category_spend,
            "pending_actions": _pending_for_user(request.user).count(),
            "can_manage": can_manage_vouchers(request.user),
            "can_view_all_branches": can_view_all_branches(request.user),
            "branches_enabled": branch_features_enabled(),
        },
    )


@login_required
def voucher_list(request):
    form = VoucherFilterForm(
        request.GET or None,
        user=request.user,
        branches_enabled=branch_features_enabled(),
    )
    queryset = branch_scope(
        Voucher.objects.select_related(
            "category", "prepared_by", "recommended_by", "approved_by", "authorized_by", "approver", "branch"
        ),
        request.user,
    )
    if not can_approve_any(request.user):
        queryset = queryset.filter(
            Q(prepared_by=request.user)
            | Q(status=Voucher.Status.PREPARED, approver=request.user)
            | Q(status__in=_role_statuses(request.user), approver__isnull=True)
            | Q(audit_logs__actor=request.user)
        ).distinct()
    if form.is_valid():
        data = form.cleaned_data
        if data.get("q"):
            query = data["q"].strip()
            queryset = queryset.filter(
                Q(ref_no__icontains=query)
                | Q(to__icontains=query)
                | Q(payee__icontains=query)
                | Q(being__icontains=query)
                | Q(cheque_number__icontains=query)
                | Q(account_details__icontains=query)
            )
        if data.get("status"):
            queryset = queryset.filter(status=data["status"])
        if data.get("category"):
            queryset = queryset.filter(category=data["category"])
        if data.get("method"):
            queryset = queryset.filter(method_of_payment=data["method"])
        if data.get("date_from"):
            queryset = queryset.filter(date__gte=data["date_from"])
        if data.get("date_to"):
            queryset = queryset.filter(date__lte=data["date_to"])
        if data.get("branch") and can_view_all_branches(request.user):
            queryset = queryset.filter(branch=data["branch"])
    return render(
        request,
        "vouchers/voucher_list.html",
        {
            "filter_form": form,
            "vouchers": queryset[:250],
            "can_manage": can_manage_vouchers(request.user),
            "branches_enabled": branch_features_enabled(),
        },
    )


@login_required
def approval_queue(request):
    if not (
        can_approve_any(request.user)
        or request.user.has_perm("vouchers.approve_voucher")
        or _role_statuses(request.user)
    ):
        raise PermissionDenied
    vouchers = _pending_for_user(request.user).order_by("date", "created_at")
    return render(request, "vouchers/approval_queue.html", {"vouchers": vouchers})


@login_required
def approver_options(request):
    if not can_manage_vouchers(request.user):
        raise PermissionDenied
    branch = None
    if branch_features_enabled():
        branch_id = request.GET.get("branch")
        if not branch_id:
            return JsonResponse({"error": "Select a branch first."}, status=400)
        branch = get_object_or_404(
            Branch.objects.filter(is_active=True), pk=branch_id
        )
        assigned_branch = user_branch(request.user)
        if (
            not can_view_all_branches(request.user)
            and (not assigned_branch or assigned_branch.pk != branch.pk)
        ):
            raise PermissionDenied
    approvers = eligible_approvers(branch)
    return JsonResponse(
        {
            "approvers": [
                {
                    "id": user.pk,
                    "label": user.get_full_name() or user.get_username(),
                }
                for user in approvers
            ]
        }
    )


@login_required
def voucher_create(request):
    if not can_manage_vouchers(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = VoucherForm(
            request.POST,
            request.FILES,
            user=request.user,
            branches_enabled=branch_features_enabled(),
        )
        if form.is_valid():
            voucher = form.save(commit=False)
            voucher.prepared_by = request.user
            if not branch_features_enabled():
                voucher.branch = None
            voucher.save()
            record_audit(voucher, request.user, "created", ip_address=_client_ip(request))
            try:
                transition_voucher(
                    voucher.pk,
                    request.user,
                    "submit",
                    ip_address=_client_ip(request),
                )
            except NotificationDeliveryError as exc:
                messages.warning(request, str(exc))
            except WorkflowError as exc:
                messages.error(request, str(exc))
                return redirect("voucher_detail", pk=voucher.pk)
            else:
                voucher.refresh_from_db()
                if voucher.status == Voucher.Status.NO_APPROVAL:
                    messages.success(
                        request,
                        f"Voucher {voucher.ref_no} was created without approval and is ready to print.",
                    )
                else:
                    messages.success(
                        request,
                        f"Voucher {voucher.ref_no} was submitted to {voucher.approver}.",
                    )
            return redirect("voucher_detail", pk=voucher.pk)
    else:
        form = VoucherForm(
            user=request.user, branches_enabled=branch_features_enabled()
        )
    return render(
        request,
        "vouchers/voucher_form.html",
        {
            "form": form,
            "title": "Create voucher",
            "branches_enabled": branch_features_enabled(),
            "has_branch_choices": (
                form.fields["branch"].queryset.exists()
                if "branch" in form.fields
                else True
            ),
        },
    )


@login_required
def voucher_update(request, pk):
    voucher = get_object_or_404(Voucher, pk=pk)
    if (
        not can_view_voucher(request.user, voucher)
        or voucher.status != Voucher.Status.DRAFT
        or not can_manage_vouchers(request.user)
        or (
            voucher.prepared_by_id != request.user.id
            and not user_in_role(request.user, "Finance Admins")
        )
    ):
        raise PermissionDenied
    if request.method == "POST":
        form = VoucherForm(
            request.POST,
            request.FILES,
            instance=voucher,
            user=request.user,
            branches_enabled=branch_features_enabled(),
        )
        if form.is_valid():
            form.save()
            record_audit(voucher, request.user, "updated", ip_address=_client_ip(request))
            messages.success(request, "Draft voucher updated.")
            return redirect("voucher_detail", pk=voucher.pk)
    else:
        form = VoucherForm(
            instance=voucher,
            user=request.user,
            branches_enabled=branch_features_enabled(),
        )
    return render(
        request,
        "vouchers/voucher_form.html",
        {
            "form": form,
            "title": f"Edit {voucher.ref_no}",
            "voucher": voucher,
            "branches_enabled": branch_features_enabled(),
            "has_branch_choices": (
                form.fields["branch"].queryset.exists()
                if "branch" in form.fields
                else True
            ),
        },
    )


@login_required
def voucher_detail(request, pk):
    voucher = get_object_or_404(
        Voucher.objects.select_related(
            "category", "prepared_by", "recommended_by", "approved_by", "authorized_by", "approver", "branch"
        ).prefetch_related("audit_logs__actor"),
        pk=pk,
    )
    if not can_view_voucher(request.user, voucher):
        raise PermissionDenied
    can_act = _can_act_on_voucher(request.user, voucher)
    return render(
        request,
        "vouchers/voucher_detail.html",
        {
            "voucher": voucher,
            "can_act": can_act,
            "can_edit": voucher.status == Voucher.Status.DRAFT
            and voucher.prepared_by_id == request.user.id
            and can_manage_vouchers(request.user),
            "can_print": voucher.is_printable,
            "rejection_form": RejectionForm(),
            "authorization_form": AuthorizationForm(),
        },
    )


@login_required
@require_POST
def voucher_action(request, pk, action):
    voucher = get_object_or_404(Voucher, pk=pk)
    if not can_view_voucher(request.user, voucher):
        raise PermissionDenied
    if action not in {"prepare", "submit", "approve", "reject"}:
        raise PermissionDenied
    form = RejectionForm(request.POST)
    authorization_form = AuthorizationForm(request.POST, request.FILES)
    notes = ""
    authorized_signature = None
    if action == "reject":
        if not form.is_valid():
            for error in form.errors.get("reason", []):
                messages.error(request, error)
            return redirect("voucher_detail", pk=pk)
        notes = form.cleaned_data["reason"]
    elif action == "approve" and voucher.status == Voucher.Status.APPROVED:
        if not authorization_form.is_valid():
            for error in authorization_form.errors.get("authorized_signature", []):
                messages.error(request, error)
            return redirect("voucher_detail", pk=pk)
        authorized_signature = authorization_form.cleaned_data.get("authorized_signature")
    try:
        updated = transition_voucher(
            pk,
            request.user,
            action,
            notes=notes,
            ip_address=_client_ip(request),
            authorized_signature=authorized_signature,
        )
        messages.success(
            request, f"{updated.ref_no} is now {updated.get_status_display().lower()}."
        )
    except NotificationDeliveryError as exc:
        messages.warning(request, str(exc))
    except WorkflowError as exc:
        messages.error(request, str(exc))
    return redirect("voucher_detail", pk=pk)


@login_required
def voucher_pdf(request, pk):
    voucher = get_object_or_404(
        Voucher.objects.select_related(
            "category", "prepared_by", "recommended_by", "approved_by", "authorized_by"
        ),
        pk=pk,
    )
    if not can_view_voucher(request.user, voucher):
        raise PermissionDenied
    if not voucher.is_printable:
        raise PermissionDenied("A voucher cannot be printed before approval.")

    branding = CompanySettings.load()
    pdf_colors = {
        "primary": colors.HexColor(branding.primary_color),
        "secondary": colors.HexColor(branding.secondary_color),
        "accent": colors.HexColor(branding.accent_color),
    }
    is_landscape = branding.print_layout == CompanySettings.PrintLayout.PAPER_FORM
    page_size = landscape(A4) if is_landscape else A4
    compact = branding.print_layout == CompanySettings.PrintLayout.COMPACT
    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{voucher.ref_no}.pdf"'
    document = SimpleDocTemplate(
        response,
        pagesize=page_size,
        rightMargin=10 * mm if compact else 16 * mm,
        leftMargin=10 * mm if compact else 16 * mm,
        topMargin=10 * mm if compact else 13 * mm,
        bottomMargin=10 * mm if compact else 13 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    title_style.textColor = pdf_colors["primary"]
    title_style.fontSize = 17 if compact else 20
    subtitle_style = styles["Heading2"]
    subtitle_style.textColor = pdf_colors["secondary"]
    story = []
    header_cells = []
    if branding.logo:
        with branding.logo.open("rb") as logo_file:
            logo_image = Image(
                BytesIO(logo_file.read()),
                width=35 * mm,
                height=18 * mm,
                kind="proportional",
            )
        header_cells.append(logo_image)
    company_lines = [f"<b>{escape(branding.company_name)}</b>"]
    if branding.header_text:
        company_lines.append(escape(branding.header_text))
    company_lines.append("<b>PAYMENT VOUCHER</b>")
    company_paragraph = Paragraph("<br/>".join(company_lines), title_style)
    if header_cells:
        header = Table([[header_cells[0], company_paragraph]], colWidths=[42 * mm, None])
        header.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LINEBELOW", (0, 0), (-1, -1), 2, pdf_colors["accent"]),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ]
            )
        )
        story.append(header)
    else:
        story.append(company_paragraph)

    def signature_cell(signature):
        if not signature:
            return "-"
        with signature.open("rb") as signature_file:
            return Image(
                BytesIO(signature_file.read()),
                width=25 * mm,
                height=12 * mm,
                kind="proportional",
            )

    story.extend(
        [
            Paragraph(f"{voucher.ref_no} · {voucher.get_status_display()}", subtitle_style),
            Spacer(1, 5 * mm),
        ]
    )
    rows = [
        ["Ref No.", voucher.ref_no, "Date", voucher.date.strftime("%d-%m-%Y")],
        ["Amount", f"{branding.currency_symbol} {voucher.amount:,.2f}", "Status", voucher.get_status_display()],
        ["Cash", "Yes" if voucher.cash else "No", "Method of Payment", voucher.get_method_of_payment_display()],
        ["To", voucher.to, "Account Details", voucher.account_details or "-"],
        ["Cheque No.", voucher.cheque_number or "-", "Receipt Attached", "Yes" if voucher.receipt_attached else "No"],
        ["Being", voucher.being, "Category", voucher.category.name],
        ["Branch", voucher.branch.name if voucher.branch else "-", "Payee", voucher.payee],
        ["Approval Required", "Yes" if voucher.requires_approval else "No", "Assigned Approver", voucher.approver.get_full_name() if voucher.approver else "-"],
        ["Prepared By", voucher.prepared_by.get_full_name() or voucher.prepared_by.username, "Submitted At", voucher.approval_requested_at.strftime("%d-%m-%Y %H:%M") if voucher.approval_requested_at else "-"],
        ["Approved By", voucher.approved_by.get_full_name() if voucher.approved_by else "-", "Authorized By", voucher.authorized_by.get_full_name() if voucher.authorized_by else "-"],
        ["Payee Signature", signature_cell(voucher.signature), "Authorized Signature", signature_cell(voucher.authorized_signature)],
    ]
    if is_landscape:
        col_widths = [32 * mm, 91 * mm, 38 * mm, 89 * mm]
    else:
        col_widths = [30 * mm, 58 * mm, 30 * mm, 58 * mm]
    table = Table(rows, colWidths=col_widths, repeatRows=0)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.6, pdf_colors["accent"]),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2F5F2")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F2F5F2")),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5 if compact else 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5 if compact else 8),
            ]
        )
    )
    story.extend([table, Spacer(1, 8 * mm)])
    if voucher.receipt_file:
        story.append(Paragraph("Receipt file: attached electronically", styles["Normal"]))
    if branding.footer_text:
        footer_style = styles["Normal"]
        footer_style.textColor = pdf_colors["secondary"]
        story.extend(
            [
                Spacer(1, 6 * mm),
                Paragraph(escape(branding.footer_text), footer_style),
            ]
        )
    document.build(story)
    return response


@login_required
def voucher_print(request, pk):
    voucher = get_object_or_404(
        Voucher.objects.select_related(
            "category", "prepared_by", "recommended_by", "approved_by", "authorized_by"
        ),
        pk=pk,
    )
    if not can_view_voucher(request.user, voucher):
        raise PermissionDenied
    if not voucher.is_printable:
        raise PermissionDenied("A voucher cannot be printed before approval.")
    return render(request, "vouchers/voucher_print.html", {"voucher": voucher})


@login_required
def company_logo(request):
    branding = CompanySettings.load()
    if not branding.logo:
        raise PermissionDenied
    response = FileResponse(branding.logo.open("rb"))
    response["Content-Disposition"] = (
        f'inline; filename="{Path(branding.logo.name).name}"'
    )
    return response


@login_required
def company_settings(request):
    if not (
        request.user.is_superuser
        or user_in_role(request.user, "Finance Admins")
        or request.user.has_perm("vouchers.change_companysettings")
    ):
        raise PermissionDenied
    branding = CompanySettings.load()
    if request.method == "POST":
        form = CompanySettingsForm(request.POST, request.FILES, instance=branding)
        if form.is_valid():
            form.save()
            messages.success(request, "Company branding and print settings were saved.")
            return redirect("company_settings")
    else:
        form = CompanySettingsForm(instance=branding)
    color_fields = [
        form[name]
        for name in ("primary_color", "secondary_color", "accent_color")
    ]
    return render(
        request,
        "vouchers/company_settings.html",
        {
            "form": form,
            "branding": branding,
            "color_fields": color_fields,
            "can_manage_branches": can_manage_branches(request.user),
        },
    )


@login_required
def branch_list(request):
    if not can_manage_branches(request.user):
        raise PermissionDenied
    return render(
        request,
        "vouchers/branch_list.html",
        {
            "branches": Branch.objects.all(),
            "branches_enabled": branch_features_enabled(),
        },
    )


@login_required
def branch_create(request):
    if not can_manage_branches(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = BranchForm(request.POST)
        if form.is_valid():
            branch = form.save()
            messages.success(request, f"Branch {branch.name} was created.")
            return redirect("branch_list")
    else:
        form = BranchForm()
    return render(
        request,
        "vouchers/branch_form.html",
        {"form": form, "title": "Create branch"},
    )


@login_required
def branch_update(request, pk):
    if not can_manage_branches(request.user):
        raise PermissionDenied
    branch = get_object_or_404(Branch, pk=pk)
    if request.method == "POST":
        form = BranchForm(request.POST, instance=branch)
        if form.is_valid():
            form.save()
            messages.success(request, f"Branch {branch.name} was updated.")
            return redirect("branch_list")
    else:
        form = BranchForm(instance=branch)
    return render(
        request,
        "vouchers/branch_form.html",
        {"form": form, "title": f"Edit branch: {branch.name}", "branch": branch},
    )


@login_required
@require_POST
def branch_toggle(request, pk):
    if not can_manage_branches(request.user):
        raise PermissionDenied
    branch = get_object_or_404(Branch, pk=pk)
    branch.is_active = not branch.is_active
    branch.save(update_fields=["is_active"])
    messages.success(
        request,
        f"Branch {branch.name} was {'activated' if branch.is_active else 'deactivated'}. "
        "Existing vouchers and financial records were retained.",
    )
    return redirect("branch_list")


@login_required
def voucher_attachment(request, pk, kind):
    voucher = get_object_or_404(Voucher, pk=pk)
    if not can_view_voucher(request.user, voucher):
        raise PermissionDenied
    files = {
        "receipt": voucher.receipt_file,
        "signature": voucher.signature,
        "authorized-signature": voucher.authorized_signature,
    }
    attachment = files.get(kind)
    if not attachment:
        raise PermissionDenied
    response = FileResponse(attachment.open("rb"), as_attachment=True)
    response["Content-Disposition"] = (
        f'attachment; filename="{Path(attachment.name).name}"'
    )
    return response


@login_required
def reports(request):
    if not (
        request.user.is_superuser
        or user_in_role(request.user, "Finance Admins")
        or request.user.has_perm("vouchers.view_finance_reports")
    ):
        raise PermissionDenied
    today = timezone.localdate()
    month_start = today.replace(day=1)
    authorized = Voucher.objects.filter(
        status__in=[
            Voucher.Status.APPROVED,
            Voucher.Status.AUTHORIZED,
            Voucher.Status.NO_APPROVAL,
        ]
    )
    authorized = branch_scope(authorized, request.user)
    month_vouchers = authorized.filter(date__gte=month_start, date__lte=today)
    category_report = (
        month_vouchers.values("category__name")
        .annotate(total=Sum("amount"), count=Count("pk"))
        .order_by("-total")
    )
    branch_report = (
        month_vouchers.values("branch__name", "branch__code")
        .annotate(total=Sum("amount"), count=Count("pk"))
        .order_by("branch__name")
    )
    budget_rows = []
    for category in ExpenseCategory.objects.filter(is_active=True):
        spent = (
            month_vouchers.filter(category=category).aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00")
        )
        limit = category.monthly_budget_limit
        budget_rows.append(
            {
                "category": category,
                "spent": spent,
                "remaining": max(limit - spent, Decimal("0.00"))
                if limit > 0
                else None,
                "percentage": min(float(spent / limit * 100), 100.0)
                if limit > 0
                else None,
            }
        )
    return render(
        request,
        "vouchers/reports.html",
        {
            "month_total": month_vouchers.aggregate(total=Sum("amount"))["total"]
            or Decimal("0.00"),
            "month_count": month_vouchers.count(),
            "category_report": category_report,
            "branch_report": branch_report,
            "branches_enabled": branch_features_enabled(),
            "budget_rows": budget_rows,
            "budgeted_category_count": ExpenseCategory.objects.filter(
                is_active=True, monthly_budget_limit__gt=0
            ).count(),
            "report_month": today.strftime("%B %Y"),
        },
    )


@login_required
def recurring_list(request):
    if not can_manage_recurring(request.user):
        raise PermissionDenied
    templates = branch_scope(
        RecurringVoucher.objects.select_related("category", "prepared_by"),
        request.user,
    )
    return render(
        request,
        "vouchers/recurring_list.html",
        {"recurring_vouchers": templates},
    )


@login_required
def recurring_create(request):
    if not can_manage_recurring(request.user):
        raise PermissionDenied
    if request.method == "POST":
        form = RecurringVoucherForm(
            request.POST,
            user=request.user,
            branches_enabled=branch_features_enabled(),
        )
        if form.is_valid():
            recurring = form.save(commit=False)
            recurring.prepared_by = request.user
            if not branch_features_enabled():
                recurring.branch = None
            recurring.save()
            messages.success(request, "Recurring voucher schedule created.")
            return redirect("recurring_list")
    else:
        form = RecurringVoucherForm(
            user=request.user, branches_enabled=branch_features_enabled()
        )
    return render(
        request,
        "vouchers/recurring_form.html",
        {
            "form": form,
            "title": "Create recurring voucher",
            "branches_enabled": branch_features_enabled(),
        },
    )


@login_required
@require_POST
def recurring_toggle(request, pk):
    if not can_manage_recurring(request.user):
        raise PermissionDenied
    recurring = get_object_or_404(
        branch_scope(RecurringVoucher.objects.all(), request.user), pk=pk
    )
    recurring.is_active = not recurring.is_active
    recurring.save(update_fields=["is_active"])
    messages.success(
        request,
        f"Schedule {recurring.title} {'activated' if recurring.is_active else 'paused'}.",
    )
    return redirect("recurring_list")


def _require_permission(user, permission):
    if not (user.is_superuser or user.has_perm(permission)):
        raise PermissionDenied


@login_required
def user_list(request):
    _require_permission(request.user, "auth.view_user")
    users = (
        User.objects.select_related(
            "voucher_profile__branch", "voucher_profile__role_group"
        )
        .prefetch_related(
            Prefetch(
                "groups",
                queryset=Group.objects.filter(name__in=ROLE_GROUPS),
                to_attr="workflow_roles",
            )
        )
        .order_by("username")
    )
    query = request.GET.get("q", "").strip()
    if query:
        users = users.filter(
            Q(username__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(email__icontains=query)
            | Q(voucher_profile__branch__name__icontains=query)
        )
    return render(
        request,
        "vouchers/user_list.html",
        {
            "users": users[:250],
            "query": query,
            "can_add_user": request.user.is_superuser
            or request.user.has_perm("auth.add_user"),
            "can_manage_groups": request.user.is_superuser
            or request.user.has_perm("auth.view_group"),
            "role_groups": ROLE_GROUPS,
        },
    )


@login_required
def user_create(request):
    _require_permission(request.user, "auth.add_user")
    if request.method == "POST":
        form = UserManagementForm(request.POST, actor=request.user)
        if form.is_valid():
            user = form.save()
            messages.success(request, f"User {user.username} was created.")
            return redirect("user_list")
    else:
        form = UserManagementForm(actor=request.user)
    return render(
        request,
        "vouchers/user_form.html",
        {"form": form, "title": "Add user", "is_creating": True},
    )


@login_required
def user_update(request, pk):
    _require_permission(request.user, "auth.change_user")
    managed_user = get_object_or_404(
        User.objects.select_related("voucher_profile__branch", "voucher_profile__role_group"),
        pk=pk,
    )
    if managed_user.is_superuser and not request.user.is_superuser:
        raise PermissionDenied
    if managed_user.pk == request.user.pk and request.method == "POST":
        if request.POST.get("is_active") != "on":
            raise PermissionDenied("You cannot deactivate your own account.")
    if request.method == "POST":
        form = UserManagementForm(
            request.POST, instance=managed_user, actor=request.user
        )
        if form.is_valid():
            form.save()
            messages.success(request, f"User {managed_user.username} was updated.")
            return redirect("user_list")
    else:
        form = UserManagementForm(instance=managed_user, actor=request.user)
    return render(
        request,
        "vouchers/user_form.html",
        {
            "form": form,
            "title": f"Edit {managed_user.get_full_name() or managed_user.username}",
            "managed_user": managed_user,
            "is_creating": False,
        },
    )


@login_required
def group_list(request):
    _require_permission(request.user, "auth.view_group")
    groups = Group.objects.prefetch_related("permissions").order_by("name")
    return render(
        request,
        "vouchers/group_list.html",
        {
            "groups": groups,
            "can_add_group": request.user.is_superuser
            or request.user.has_perm("auth.add_group"),
            "can_change_group": request.user.is_superuser
            or request.user.has_perm("auth.change_group"),
            "role_groups": ROLE_GROUPS,
        },
    )


@login_required
def group_create(request):
    _require_permission(request.user, "auth.add_group")
    if request.method == "POST":
        form = GroupManagementForm(request.POST, actor=request.user)
        if form.is_valid():
            group = form.save()
            messages.success(request, f"Group {group.name} was created.")
            return redirect("group_list")
    else:
        form = GroupManagementForm(actor=request.user)
    return render(
        request,
        "vouchers/group_form.html",
        {"form": form, "title": "Create group"},
    )


@login_required
def group_update(request, pk):
    _require_permission(request.user, "auth.change_group")
    group = get_object_or_404(Group, pk=pk)
    if group.name in ROLE_GROUPS and not request.user.is_superuser:
        raise PermissionDenied("Only a superuser can edit workflow role groups.")
    if request.method == "POST":
        form = GroupManagementForm(request.POST, instance=group, actor=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, f"Group {group.name} was updated.")
            return redirect("group_list")
    else:
        form = GroupManagementForm(instance=group, actor=request.user)
    return render(
        request,
        "vouchers/group_form.html",
        {"form": form, "title": f"Edit group: {group.name}", "group": group},
    )
