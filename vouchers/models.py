from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db import transaction
from django.utils import timezone


class CompanySettings(models.Model):
    class Currency(models.TextChoices):
        INR = "INR", "Indian Rupee (Rs.)"
        LKR = "LKR", "Sri Lankan Rupee (LKR)"
        USD = "USD", "US Dollar ($)"
        EUR = "EUR", "Euro (€)"
        GBP = "GBP", "British Pound (£)"
        AED = "AED", "UAE Dirham (AED)"
        SGD = "SGD", "Singapore Dollar (S$)"
        BDT = "BDT", "Bangladeshi Taka (৳)"
        NPR = "NPR", "Nepalese Rupee (NPR)"

    class Theme(models.TextChoices):
        FOREST = "forest", "Forest"
        OCEAN = "ocean", "Ocean"
        CLAY = "clay", "Clay"
        SLATE = "slate", "Slate"
        CUSTOM = "custom", "Custom colors"

    class PrintLayout(models.TextChoices):
        PAPER_FORM = "paper_form", "Landscape paper voucher"
        PORTRAIT = "portrait", "Portrait voucher"
        COMPACT = "compact", "Compact voucher"

    company_name = models.CharField(
        max_length=180,
        default="Heritage Crops Plantation and Exports Pvt Ltd",
    )
    currency_code = models.CharField(
        max_length=3,
        choices=Currency.choices,
        default=Currency.INR,
    )
    logo = models.ImageField(upload_to="company/", blank=True)
    theme_name = models.CharField(
        max_length=20, choices=Theme.choices, default=Theme.FOREST
    )
    primary_color = models.CharField(
        max_length=7,
        default="#254C3B",
        validators=[RegexValidator(r"^#[0-9A-Fa-f]{6}$")],
    )
    secondary_color = models.CharField(
        max_length=7,
        default="#19382C",
        validators=[RegexValidator(r"^#[0-9A-Fa-f]{6}$")],
    )
    accent_color = models.CharField(
        max_length=7,
        default="#6C9B72",
        validators=[RegexValidator(r"^#[0-9A-Fa-f]{6}$")],
    )
    header_text = models.CharField(max_length=250, blank=True)
    footer_text = models.CharField(
        max_length=300,
        default="Heritage Crops Plantation and Exports Pvt Ltd",
        blank=True,
    )
    print_layout = models.CharField(
        max_length=20,
        choices=PrintLayout.choices,
        default=PrintLayout.PAPER_FORM,
    )
    branches_enabled = models.BooleanField(
        default=False,
        help_text="Enable branch assignment and branch-scoped voucher access.",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "company branding and print settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        settings_obj, _ = cls.objects.get_or_create(pk=1)
        return settings_obj

    def __str__(self):
        return f"Branding and print settings for {self.company_name}"

    @property
    def currency_symbol(self):
        return {
            self.Currency.INR: "Rs.",
            self.Currency.LKR: "LKR",
            self.Currency.USD: "$",
            self.Currency.EUR: "€",
            self.Currency.GBP: "£",
            self.Currency.AED: "AED",
            self.Currency.SGD: "S$",
            self.Currency.BDT: "৳",
            self.Currency.NPR: "NPR",
        }[self.currency_code]


class Branch(models.Model):
    name = models.CharField(max_length=120, unique=True)
    code = models.CharField(
        max_length=12,
        unique=True,
        validators=[RegexValidator(r"^[A-Z0-9]{2,12}$")],
    )
    address = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        permissions = [
            ("view_all_branches", "Can view vouchers across all branches"),
            ("view_finance_reports", "Can view finance reports"),
            ("manage_branches", "Can manage branches"),
        ]

    def __str__(self):
        return f"{self.name} ({self.code})"


class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="voucher_profile"
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="user_profiles",
        null=True,
        blank=True,
    )
    role_group = models.ForeignKey(
        Group,
        on_delete=models.SET_NULL,
        related_name="primary_role_profiles",
        null=True,
        blank=True,
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Profile for {self.user}"


class ExpenseCategory(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    monthly_budget_limit = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Set to zero for no monthly limit.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "expense categories"

    def __str__(self):
        return self.name


class RecurringVoucher(models.Model):
    class Frequency(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        MONTHLY = "monthly", "Monthly"
        QUARTERLY = "quarterly", "Quarterly"
        YEARLY = "yearly", "Yearly"

    title = models.CharField(max_length=150)
    category = models.ForeignKey(
        ExpenseCategory, on_delete=models.PROTECT, related_name="recurring_vouchers"
    )
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    frequency = models.CharField(max_length=12, choices=Frequency.choices)
    next_run = models.DateField()
    cash = models.BooleanField(default=False)
    method_of_payment = models.CharField(
        max_length=20,
        choices=[
            ("cash", "Cash"),
            ("cheque", "Cheque"),
            ("bank_transfer", "Bank transfer"),
            ("online", "Online"),
        ],
    )
    to = models.CharField(max_length=200)
    account_details = models.CharField(max_length=250, blank=True)
    being = models.CharField(max_length=250)
    payee = models.CharField(max_length=150)
    requires_approval = models.BooleanField(default=True)
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assigned_recurring_vouchers",
        null=True,
        blank=True,
    )
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recurring_vouchers",
    )
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="recurring_vouchers",
        null=True,
        blank=True,
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["next_run", "title"]

    def __str__(self):
        return self.title


class Voucher(models.Model):
    class PaymentMethod(models.TextChoices):
        CASH = "cash", "Cash"
        CHEQUE = "cheque", "Cheque"
        BANK_TRANSFER = "bank_transfer", "Bank transfer"
        ONLINE = "online", "Online"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PREPARED = "prepared", "Awaiting approval"
        RECOMMENDED = "recommended", "Recommended"
        APPROVED = "approved", "Approved"
        AUTHORIZED = "authorized", "Authorized"
        REJECTED = "rejected", "Rejected"
        NO_APPROVAL = "no_approval", "No approval required"

    ref_no = models.CharField(max_length=30, unique=True, blank=True)
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    date = models.DateField(default=timezone.localdate)
    cash = models.BooleanField(default=False)
    method_of_payment = models.CharField(max_length=20, choices=PaymentMethod.choices)
    to = models.CharField(max_length=200, verbose_name="To")
    account_details = models.CharField(max_length=250, blank=True)
    cheque_number = models.CharField(max_length=50, blank=True)
    receipt_attached = models.BooleanField(default=False)
    receipt_file = models.FileField(upload_to="vouchers/receipts/%Y/%m/", blank=True)
    being = models.CharField(max_length=250)
    payee = models.CharField(max_length=150)
    category = models.ForeignKey(
        ExpenseCategory, on_delete=models.PROTECT, related_name="vouchers"
    )
    requires_approval = models.BooleanField(default=True)
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="assigned_vouchers",
        null=True,
        blank=True,
    )
    approval_requested_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    branch = models.ForeignKey(
        Branch,
        on_delete=models.PROTECT,
        related_name="vouchers",
        null=True,
        blank=True,
        db_index=True,
    )
    signature = models.FileField(upload_to="vouchers/signatures/payee/%Y/%m/", blank=True)
    authorized_signature = models.FileField(
        upload_to="vouchers/signatures/authorized/%Y/%m/", blank=True
    )
    prepared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="prepared_vouchers",
    )
    recommended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recommended_vouchers",
        null=True,
        blank=True,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="approved_vouchers",
        null=True,
        blank=True,
    )
    authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="authorized_vouchers",
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    rejection_reason = models.TextField(blank=True)
    recurring_template = models.ForeignKey(
        RecurringVoucher,
        on_delete=models.SET_NULL,
        related_name="generated_vouchers",
        null=True,
        blank=True,
    )
    recurring_period_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        permissions = [
            ("approve_voucher", "Can approve assigned payment vouchers"),
            ("approve_any_voucher", "Can approve any payment voucher"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["recurring_template", "recurring_period_date"],
                name="unique_recurring_voucher_period",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="voucher_amount_must_be_positive",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.ref_no:
            year = self.date.year if self.date else timezone.localdate().year
            branch = Branch.objects.get(pk=self.branch_id) if self.branch_id else None
            prefix = (
                f"HCP-{branch.code}-{year}-" if branch else f"HCP-{year}-"
            )
            scope = f"branch:{branch.pk}" if branch else "global"
            last_ref = (
                Voucher.objects.filter(ref_no__startswith=prefix)
                .exclude(pk=self.pk)
                .order_by("-ref_no")
                .values_list("ref_no", flat=True)
                .first()
            )
            initial_sequence = int(last_ref.rsplit("-", 1)[-1]) if last_ref else 0
            with transaction.atomic():
                sequence, _ = VoucherSequence.objects.select_for_update().get_or_create(
                    scope=scope,
                    year=year,
                    defaults={"last_value": initial_sequence},
                )
                sequence.last_value = max(sequence.last_value, initial_sequence) + 1
                sequence.save(update_fields=["last_value"])
                self.ref_no = f"{prefix}{sequence.last_value:05d}"
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)

    @property
    def is_terminal(self):
        return self.status in {
            self.Status.APPROVED,
            self.Status.AUTHORIZED,
            self.Status.REJECTED,
            self.Status.NO_APPROVAL,
        }

    @property
    def is_printable(self):
        return self.status in {
            self.Status.APPROVED,
            self.Status.AUTHORIZED,
            self.Status.NO_APPROVAL,
        }

    def __str__(self):
        return self.ref_no or "New voucher"


class AuditLog(models.Model):
    voucher = models.ForeignKey(
        Voucher, on_delete=models.CASCADE, related_name="audit_logs"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="voucher_audit_logs"
    )
    action = models.CharField(max_length=30)
    previous_status = models.CharField(max_length=12, blank=True)
    new_status = models.CharField(max_length=12, blank=True)
    notes = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.voucher.ref_no}: {self.action}"


class VoucherSequence(models.Model):
    scope = models.CharField(max_length=30)
    year = models.PositiveSmallIntegerField()
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["scope", "year"], name="unique_voucher_sequence_scope_year"
            )
        ]

    def __str__(self):
        return f"{self.scope} {self.year}: {self.last_value}"
