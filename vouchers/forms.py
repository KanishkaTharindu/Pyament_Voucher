from pathlib import Path

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import (
    Branch,
    CompanySettings,
    ExpenseCategory,
    RecurringVoucher,
    UserProfile,
    Voucher,
)
from .services import (
    ROLE_GROUPS,
    can_view_all_branches,
    eligible_approvers,
    user_branch,
)

User = get_user_model()


class BootstrapFormMixin:
    def apply_bootstrap(self):
        for field in self.fields.values():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-check-input"
            elif isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select"
            else:
                field.widget.attrs["class"] = "form-control"
        for name in ("receipt_file", "signature", "authorized_signature"):
            if name in self.fields:
                self.fields[name].widget.attrs["accept"] = ".pdf,.png,.jpg,.jpeg"

    def clean_upload(self, field_name, allowed_extensions):
        uploaded = self.cleaned_data.get(field_name)
        if uploaded:
            extension = Path(uploaded.name).suffix.lower()
            if extension not in allowed_extensions:
                raise ValidationError(
                    f"Choose a file with one of these extensions: {', '.join(sorted(allowed_extensions))}."
                )
            if uploaded.size > 10 * 1024 * 1024:
                raise ValidationError("Files must be 10 MB or smaller.")
        return uploaded


class VoucherForm(BootstrapFormMixin, forms.ModelForm):
    approver = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="Select an authorized approver",
        label="Approver",
    )

    class Meta:
        model = Voucher
        fields = [
            "amount",
            "date",
            "cash",
            "method_of_payment",
            "to",
            "account_details",
            "cheque_number",
            "receipt_attached",
            "receipt_file",
            "being",
            "payee",
            "category",
            "requires_approval",
            "approver",
            "branch",
            "signature",
        ]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "being": forms.TextInput(attrs={"placeholder": "Purpose of payment"}),
            "account_details": forms.TextInput(
                attrs={"placeholder": "Account name, number or bank details"}
            ),
        }

    def __init__(self, *args, user=None, branches_enabled=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.branches_enabled = branches_enabled
        self.fields["category"].queryset = ExpenseCategory.objects.filter(is_active=True)
        approval_branch = self.instance.branch if self.instance.pk else None
        if branches_enabled:
            branch_queryset = Branch.objects.filter(is_active=True)
            if user and not can_view_all_branches(user):
                assigned_branch = user_branch(user)
                branch_queryset = (
                    branch_queryset.filter(pk=assigned_branch.pk)
                    if assigned_branch
                    else branch_queryset.none()
                )
            self.fields["branch"].queryset = branch_queryset
            self.fields["branch"].required = True
            if user and user_branch(user):
                self.initial.setdefault("branch", user_branch(user))
            submitted_branch = self.data.get(self.add_prefix("branch")) if self.is_bound else None
            if submitted_branch:
                approval_branch = branch_queryset.filter(pk=submitted_branch).first()
        else:
            self.fields.pop("branch")
        self.fields["approver"].queryset = (
            User.objects.none()
            if branches_enabled and approval_branch is None
            else eligible_approvers(approval_branch)
        )
        self.fields["requires_approval"].label = "Send this voucher for approval"
        self.fields["requires_approval"].help_text = (
            "If disabled, the voucher is recorded as not requiring approval and can be printed immediately."
        )
        self.fields["approver"].widget.attrs["data-approval-approver"] = "true"
        self.apply_bootstrap()

    def clean_receipt_file(self):
        return self.clean_upload("receipt_file", {".pdf", ".png", ".jpg", ".jpeg"})

    def clean_signature(self):
        return self.clean_upload("signature", {".png", ".jpg", ".jpeg"})

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("requires_approval") and not cleaned.get("approver"):
            self.add_error("approver", "Select an authorized approver.")
        if (
            cleaned.get("requires_approval")
            and self.user
            and cleaned.get("approver")
            and cleaned["approver"].pk == self.user.pk
        ):
            self.add_error("approver", "You cannot approve your own voucher.")
        if self.branches_enabled and not cleaned.get("branch"):
            self.add_error("branch", "Select an active branch.")
        if (
            cleaned.get("method_of_payment") == Voucher.PaymentMethod.CHEQUE
            and not cleaned.get("cheque_number")
        ):
            self.add_error("cheque_number", "Enter the cheque number for cheque payments.")
        if cleaned.get("cash") and cleaned.get("method_of_payment") != Voucher.PaymentMethod.CASH:
            self.add_error("cash", "Select Cash as the payment method when this is a cash voucher.")
        return cleaned


class RecurringVoucherForm(BootstrapFormMixin, forms.ModelForm):
    approver = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        empty_label="Select an authorized approver",
        label="Approver",
    )

    class Meta:
        model = RecurringVoucher
        fields = [
            "title",
            "category",
            "amount",
            "frequency",
            "next_run",
            "cash",
            "method_of_payment",
            "to",
            "account_details",
            "being",
            "payee",
            "requires_approval",
            "approver",
            "branch",
            "is_active",
        ]
        widgets = {"next_run": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, user=None, branches_enabled=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.branches_enabled = branches_enabled
        self.fields["category"].queryset = ExpenseCategory.objects.filter(is_active=True)
        branch_queryset = Branch.objects.filter(is_active=True)
        if branches_enabled:
            if user and not can_view_all_branches(user):
                assigned_branch = user_branch(user)
                branch_queryset = (
                    branch_queryset.filter(pk=assigned_branch.pk)
                    if assigned_branch
                    else branch_queryset.none()
                )
            self.fields["branch"].queryset = branch_queryset
            self.fields["branch"].required = True
            if user and user_branch(user):
                self.initial.setdefault("branch", user_branch(user))
            submitted_branch = self.data.get(self.add_prefix("branch")) if self.is_bound else None
            branch = branch_queryset.filter(pk=submitted_branch).first() if submitted_branch else None
        else:
            self.fields.pop("branch")
            branch = self.instance.branch if self.instance.pk else None
        self.fields["approver"].queryset = (
            User.objects.none()
            if branches_enabled and branch is None
            else eligible_approvers(branch)
        )
        self.fields["approver"].widget.attrs["data-approval-approver"] = "true"
        self.apply_bootstrap()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("requires_approval") and not cleaned.get("approver"):
            self.add_error("approver", "Select an authorized approver.")
        if (
            self.branches_enabled
            and not cleaned.get("branch")
        ):
            self.add_error("branch", "Select an active branch.")
        if (
            cleaned.get("requires_approval")
            and self.user
            and cleaned.get("approver")
            and cleaned["approver"].pk == self.user.pk
        ):
            self.add_error("approver", "You cannot approve your own voucher.")
        return cleaned


class RejectionForm(BootstrapFormMixin, forms.Form):
    reason = forms.CharField(
        label="Reason for rejection",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_bootstrap()


class AuthorizationForm(BootstrapFormMixin, forms.Form):
    authorized_signature = forms.FileField(
        required=False,
        label="Authorized signature",
        help_text="Optional JPG or PNG signature image, up to 10 MB.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.apply_bootstrap()
        self.fields["authorized_signature"].widget.attrs["accept"] = ".png,.jpg,.jpeg"

    def clean_authorized_signature(self):
        return BootstrapFormMixin.clean_upload(
            self, "authorized_signature", {".png", ".jpg", ".jpeg"}
        )


class VoucherFilterForm(BootstrapFormMixin, forms.Form):
    q = forms.CharField(required=False, label="Search")
    status = forms.ChoiceField(
        required=False,
        choices=[("", "All statuses"), *Voucher.Status.choices],
    )
    category = forms.ModelChoiceField(
        required=False,
        queryset=ExpenseCategory.objects.all(),
        empty_label="All categories",
    )
    method = forms.ChoiceField(
        required=False,
        choices=[("", "All payment methods"), *Voucher.PaymentMethod.choices],
    )
    date_from = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    date_to = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    branch = forms.ModelChoiceField(
        required=False,
        queryset=Branch.objects.filter(is_active=True),
        empty_label="My branch",
    )

    def __init__(self, *args, user=None, branches_enabled=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not branches_enabled or (user and not (
            user.is_superuser
            or user.has_perm("vouchers.view_all_branches")
            or user.groups.filter(name="Finance Admins").exists()
        )):
            self.fields.pop("branch")
        self.apply_bootstrap()


def _permitted_permissions(actor, current_permissions=()):
    permissions = Permission.objects.select_related("content_type").order_by(
        "content_type__app_label", "content_type__model", "codename"
    )
    if actor.is_superuser:
        return permissions
    permitted_codenames = actor.get_all_permissions()
    permitted_ids = {
        permission.pk
        for permission in permissions
        if f"{permission.content_type.app_label}.{permission.codename}"
        in permitted_codenames
    }
    permitted_ids.update(permission.pk for permission in current_permissions)
    return permissions.filter(pk__in=permitted_ids)


def _assignable_groups(actor, current_groups=()):
    groups = Group.objects.prefetch_related("permissions")
    if actor.is_superuser:
        return groups.order_by("name")
    allowed = set(actor.groups.values_list("pk", flat=True))
    allowed.update(group.pk for group in current_groups)
    return groups.filter(pk__in=allowed).order_by("name")


class UserManagementForm(BootstrapFormMixin, forms.ModelForm):
    password1 = forms.CharField(
        label="Temporary password",
        required=False,
        widget=forms.PasswordInput,
        help_text="Use at least 12 characters. Users can change it after signing in.",
    )
    password2 = forms.CharField(
        label="Confirm password",
        required=False,
        widget=forms.PasswordInput,
    )
    role = forms.ModelChoiceField(
        queryset=Group.objects.filter(name__in=ROLE_GROUPS),
        required=False,
        empty_label="Select a role",
    )
    branch = forms.ModelChoiceField(
        queryset=Branch.objects.filter(is_active=True),
        required=False,
        empty_label="Choose a branch",
    )
    groups = forms.ModelMultipleChoiceField(
        queryset=Group.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Additional groups",
    )
    user_permissions = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Direct permissions",
    )

    class Meta:
        model = User
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
            "is_active",
        )

    def __init__(self, *args, actor, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.is_creating = self.instance.pk is None
        profile = (
            UserProfile.objects.filter(user=self.instance).first()
            if not self.is_creating
            else None
        )
        current_groups = self.instance.groups.all() if not self.is_creating else Group.objects.none()
        current_role = profile.role_group if profile else None
        if not current_role:
            current_role = next(
                (group for group in current_groups if group.name in ROLE_GROUPS),
                None,
            )
        self.initial["role"] = current_role
        self.initial["branch"] = profile.branch if profile else None
        role_group_ids = [group.pk for group in current_groups if group.name in ROLE_GROUPS]
        self.initial["groups"] = current_groups.exclude(name__in=ROLE_GROUPS)
        self.initial["user_permissions"] = (
            self.instance.user_permissions.all() if not self.is_creating else Permission.objects.none()
        )
        if actor.is_superuser:
            self.fields["role"].queryset = Group.objects.filter(name__in=ROLE_GROUPS)
            self.fields["groups"].queryset = Group.objects.exclude(name__in=ROLE_GROUPS)
            self.fields["user_permissions"].queryset = _permitted_permissions(
                actor, self.instance.user_permissions.all() if self.instance.pk else ()
            )
        else:
            if actor.has_perm("auth.change_group"):
                self.fields["role"].queryset = Group.objects.filter(
                    name__in=ROLE_GROUPS
                )
            else:
                self.fields["role"].queryset = Group.objects.filter(
                    pk__in=role_group_ids, name__in=ROLE_GROUPS
                )
            self.fields["groups"].queryset = _assignable_groups(
                actor, current_groups.exclude(name__in=ROLE_GROUPS)
            ).exclude(name__in=ROLE_GROUPS)
            self.fields["user_permissions"].queryset = _permitted_permissions(
                actor, self.instance.user_permissions.all() if self.instance.pk else ()
            )
        self.apply_bootstrap()
        self.fields["groups"].widget.attrs["class"] = "permission-checkbox-list"
        self.fields["user_permissions"].widget.attrs["class"] = "permission-checkbox-list"
        self.fields["is_active"].label = "Account is active"
        if self.is_creating:
            self.fields["password1"].required = True
            self.fields["password2"].required = True

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if self.is_creating and not password1:
            self.add_error("password1", "A temporary password is required.")
        if password1 or password2:
            if password1 != password2:
                self.add_error("password2", "The passwords do not match.")
            if password1:
                try:
                    password_validation.validate_password(password1, self.instance)
                except ValidationError as exc:
                    self.add_error("password1", exc)
        if cleaned.get("role") and cleaned["role"].name not in self.fields["role"].queryset.values_list("name", flat=True):
            self.add_error("role", "You cannot assign that role.")
        return cleaned

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get("password1")
        if password:
            user.set_password(password)
        if commit:
            user.save()
            self.save_related(user)
        return user

    def save_related(self, user):
        role = self.cleaned_data.get("role")
        selected_groups = self.cleaned_data.get("groups", [])
        groups = list(selected_groups)
        if role:
            groups.append(role)
        user.groups.set(groups)
        user.user_permissions.set(self.cleaned_data.get("user_permissions", []))
        UserProfile.objects.update_or_create(
            user=user,
            defaults={
                "branch": self.cleaned_data.get("branch"),
                "role_group": role,
            },
        )


class GroupManagementForm(BootstrapFormMixin, forms.ModelForm):
    permissions = forms.ModelMultipleChoiceField(
        queryset=Permission.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Group permissions",
    )

    class Meta:
        model = Group
        fields = ("name",)

    def __init__(self, *args, actor, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        existing_permissions = self.instance.permissions.all() if self.instance.pk else ()
        self.fields["permissions"].queryset = _permitted_permissions(
            actor, existing_permissions
        )
        self.initial["permissions"] = existing_permissions
        self.apply_bootstrap()
        self.fields["permissions"].widget.attrs["class"] = "permission-checkbox-list"

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if name in ROLE_GROUPS and not self.actor.is_superuser:
            raise ValidationError("Only a superuser can create or rename a workflow role group.")
        return name

    @transaction.atomic
    def save(self, commit=True):
        group = super().save(commit=commit)
        if commit:
            group.permissions.set(self.cleaned_data.get("permissions", []))
        return group


class BranchForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = Branch
        fields = ("name", "code", "address", "is_active")
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].help_text = "Use 2-12 letters or digits; codes appear in voucher numbers."
        self.apply_bootstrap()

    def clean_code(self):
        return self.cleaned_data["code"].strip().upper()


class CompanySettingsForm(BootstrapFormMixin, forms.ModelForm):
    class Meta:
        model = CompanySettings
        fields = (
            "company_name",
            "currency_code",
            "logo",
            "theme_name",
            "primary_color",
            "secondary_color",
            "accent_color",
            "header_text",
            "footer_text",
            "print_layout",
            "branches_enabled",
        )
        widgets = {
            "primary_color": forms.TextInput(attrs={"type": "color"}),
            "secondary_color": forms.TextInput(attrs={"type": "color"}),
            "accent_color": forms.TextInput(attrs={"type": "color"}),
            "header_text": forms.TextInput(
                attrs={"placeholder": "Optional message below the company name"}
            ),
            "footer_text": forms.Textarea(
                attrs={"rows": 2, "placeholder": "Contact or footer text printed on vouchers"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["logo"].required = False
        self.fields["logo"].help_text = "PNG, JPG, or JPEG image; maximum 5 MB."
        self.fields["theme_name"].help_text = (
            "Choose a preset, then adjust the color fields to fine-tune your brand."
        )
        self.apply_bootstrap()
        for field in ("primary_color", "secondary_color", "accent_color"):
            self.fields[field].widget.attrs["class"] = "form-control form-control-color"

    def clean_logo(self):
        logo = self.cleaned_data.get("logo")
        if not logo:
            return logo
        if Path(logo.name).suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            raise ValidationError("Upload a PNG or JPEG logo.")
        if logo.size > 5 * 1024 * 1024:
            raise ValidationError("Logo files must be 5 MB or smaller.")
        return logo

    def clean_primary_color(self):
        return self._clean_hex_color("primary_color")

    def clean_secondary_color(self):
        return self._clean_hex_color("secondary_color")

    def clean_accent_color(self):
        return self._clean_hex_color("accent_color")

    def _clean_hex_color(self, name):
        value = self.cleaned_data[name]
        if len(value) != 7 or not value.startswith("#"):
            raise ValidationError("Enter a valid six-digit hex color.")
        try:
            int(value[1:], 16)
        except ValueError as exc:
            raise ValidationError("Enter a valid six-digit hex color.") from exc
        return value.upper()
