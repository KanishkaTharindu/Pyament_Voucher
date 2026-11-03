from django.contrib import admin

from .models import (
    AuditLog,
    Branch,
    CompanySettings,
    ExpenseCategory,
    RecurringVoucher,
    UserProfile,
    Voucher,
)


@admin.register(CompanySettings)
class CompanySettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        ("Company identity", {"fields": ("company_name", "currency_code", "logo", "header_text", "footer_text", "updated_at")}),
        ("Branch operations", {"fields": ("branches_enabled",)}),
        ("Color theme", {"fields": ("theme_name", "primary_color", "secondary_color", "accent_color")}),
        ("Voucher printing", {"fields": ("print_layout",)}),
    )
    readonly_fields = ("updated_at",)

    def has_add_permission(self, request):
        return not CompanySettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ExpenseCategory)
class ExpenseCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "monthly_budget_limit", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name", "code")


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "branch", "role_group", "updated_at")
    list_filter = ("branch", "role_group")
    search_fields = ("user__username", "user__email", "user__first_name", "user__last_name")


@admin.register(Voucher)
class VoucherAdmin(admin.ModelAdmin):
    list_display = (
        "ref_no",
        "date",
        "payee",
        "category",
        "amount",
        "status",
        "prepared_by",
        "branch",
    )
    list_filter = ("status", "category", "method_of_payment", "date")
    search_fields = ("ref_no", "to", "payee", "being", "cheque_number")
    readonly_fields = tuple(field.name for field in Voucher._meta.fields)
    date_hierarchy = "date"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RecurringVoucher)
class RecurringVoucherAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "amount", "frequency", "next_run", "is_active")
    list_filter = ("frequency", "is_active", "category")
    search_fields = ("title", "to", "payee")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("voucher", "actor", "action", "previous_status", "new_status", "created_at")
    list_filter = ("action", "created_at")
    search_fields = ("voucher__ref_no", "actor__username", "notes")
    readonly_fields = (
        "voucher",
        "actor",
        "action",
        "previous_status",
        "new_status",
        "notes",
        "ip_address",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
