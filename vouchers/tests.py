from datetime import date
import base64
from decimal import Decimal
from io import BytesIO
import re
from tempfile import TemporaryDirectory
import zlib

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.core import mail
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image as PillowImage

from .forms import CompanySettingsForm, GroupManagementForm, RecurringVoucherForm
from .models import (
    AuditLog,
    Branch,
    CompanySettings,
    ExpenseCategory,
    RecurringVoucher,
    UserProfile,
    Voucher,
)
from .services import WorkflowError, advance_date, transition_voucher

User = get_user_model()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class VoucherWorkflowTests(TestCase):
    def setUp(self):
        self.preparer = User.objects.create_user("preparer", password="test-password")
        self.recommender = User.objects.create_user("recommender", password="test-password")
        self.approver = User.objects.create_user("approver", password="test-password")
        self.authorizer = User.objects.create_user("authorizer", password="test-password")
        for username, group_name in (
            ("recommender", "Voucher Recommenders"),
            ("approver", "Voucher Approvers"),
            ("authorizer", "Voucher Authorizers"),
        ):
            Group.objects.get_or_create(name=group_name)
            User.objects.get(username=username).groups.add(
                Group.objects.get(name=group_name)
            )
        self.recommender.groups.add(Group.objects.get(name="Voucher Approvers"))
        self.category = ExpenseCategory.objects.create(
            name="Test office supplies", monthly_budget_limit=Decimal("1000.00")
        )
        self.voucher = Voucher.objects.create(
            amount=Decimal("250.00"),
            date=date(2026, 11, 2),
            method_of_payment=Voucher.PaymentMethod.BANK_TRANSFER,
            to="Office supplier",
            being="Stationery",
            payee="Office supplier",
            category=self.category,
            prepared_by=self.preparer,
        )

    def test_reference_is_generated_with_company_prefix(self):
        self.assertRegex(self.voucher.ref_no, r"^HCP-2026-\d{5}$")

    def test_sample_categories_are_available_in_voucher_form(self):
        from .forms import VoucherForm

        choices = list(VoucherForm().fields["category"].queryset.values_list("name", flat=True))
        self.assertIn("Plantation Operations", choices)
        self.assertIn("Office Supplies", choices)
        self.assertGreaterEqual(len(choices), 9)
        self.preparer.groups.add(Group.objects.get(name="Voucher Preparers"))
        self.client.force_login(self.preparer)
        response = self.client.get(reverse("voucher_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, ">Office Supplies</option>")

    def test_workflow_requires_distinct_role_holders_and_records_audit(self):
        self.voucher.approver = self.recommender
        self.voucher.save(update_fields=["approver"])
        transition_voucher(self.voucher.pk, self.preparer, "prepare")
        with self.assertRaisesMessage(WorkflowError, "selected approver"):
            transition_voucher(self.voucher.pk, self.preparer, "approve")
        transition_voucher(self.voucher.pk, self.recommender, "approve")
        self.voucher.refresh_from_db()
        self.assertEqual(self.voucher.status, Voucher.Status.APPROVED)
        self.assertEqual(self.voucher.approved_by, self.recommender)
        self.assertIsNotNone(self.voucher.approved_at)
        self.assertEqual(self.voucher.audit_logs.count(), 2)
        with self.assertRaisesMessage(WorkflowError, "Only the selected approver"):
            transition_voucher(self.voucher.pk, self.authorizer, "approve")

    def test_monthly_budget_blocks_authorization(self):
        self.category.monthly_budget_limit = Decimal("200.00")
        self.category.save()
        self.voucher.approver = self.recommender
        self.voucher.save(update_fields=["approver"])
        transition_voucher(self.voucher.pk, self.preparer, "prepare")
        with self.assertRaisesMessage(WorkflowError, "exceed the monthly budget"):
            transition_voucher(self.voucher.pk, self.recommender, "approve")
        self.voucher.refresh_from_db()
        self.assertEqual(self.voucher.status, Voucher.Status.PREPARED)

    def test_reject_requires_reason_and_audits_rejection(self):
        self.voucher.approver = self.recommender
        self.voucher.save(update_fields=["approver"])
        transition_voucher(self.voucher.pk, self.preparer, "prepare")
        with self.assertRaisesMessage(WorkflowError, "Enter a reason"):
            transition_voucher(self.voucher.pk, self.recommender, "reject")
        transition_voucher(
            self.voucher.pk, self.recommender, "reject", notes="Missing receipt"
        )
        self.voucher.refresh_from_db()
        self.assertEqual(self.voucher.status, Voucher.Status.REJECTED)
        self.assertEqual(self.voucher.rejection_reason, "Missing receipt")
        self.assertIsNotNone(self.voucher.rejected_at)
        self.assertEqual(AuditLog.objects.filter(action="rejected").count(), 1)

    def test_voucher_pdf_is_available_to_authorized_viewer(self):
        self.voucher.requires_approval = False
        self.voucher.status = Voucher.Status.NO_APPROVAL
        self.voucher.save(update_fields=["requires_approval", "status"])
        self.client.force_login(self.preparer)
        response = self.client.get(
            reverse("voucher_pdf", kwargs={"pk": self.voucher.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))

    def test_company_settings_form_saves_branding_and_validates_colors(self):
        settings_obj = CompanySettings.load()
        form = CompanySettingsForm(
            data={
                "company_name": "Example Plantation Ltd",
                "currency_code": CompanySettings.Currency.INR,
                "theme_name": "ocean",
                "primary_color": "#123456",
                "secondary_color": "#234567",
                "accent_color": "#345678",
                "header_text": "Plantation Division",
                "footer_text": "Registered office",
                "print_layout": CompanySettings.PrintLayout.PORTRAIT,
            },
            instance=settings_obj,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.pk, 1)
        self.assertEqual(saved.company_name, "Example Plantation Ltd")
        self.assertEqual(saved.currency_symbol, "Rs.")
        self.assertEqual(saved.primary_color, "#123456")
        self.assertEqual(saved.print_layout, CompanySettings.PrintLayout.PORTRAIT)

        invalid_form = CompanySettingsForm(
            data={
                "company_name": "Example Plantation Ltd",
                "currency_code": CompanySettings.Currency.INR,
                "theme_name": "custom",
                "primary_color": "not-a-color",
                "secondary_color": "#234567",
                "accent_color": "#345678",
                "header_text": "",
                "footer_text": "",
                "print_layout": CompanySettings.PrintLayout.PAPER_FORM,
            },
            instance=saved,
        )
        self.assertFalse(invalid_form.is_valid())
        self.assertIn("primary_color", invalid_form.errors)

    def test_company_settings_page_is_restricted_and_updates_branding(self):
        finance_group, _ = Group.objects.get_or_create(name="Finance Admins")
        self.preparer.groups.add(finance_group)
        self.client.force_login(self.preparer)
        settings_url = reverse("company_settings")
        response = self.client.get(settings_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "COLOR SYSTEM")
        response = self.client.post(
            settings_url,
            {
                "company_name": "Heritage Crops International",
                "currency_code": CompanySettings.Currency.INR,
                "theme_name": "forest",
                "primary_color": "#254C3B",
                "secondary_color": "#19382C",
                "accent_color": "#6C9B72",
                "header_text": "Plantation and exports",
                "footer_text": "Internal finance",
                "print_layout": CompanySettings.PrintLayout.COMPACT,
            },
        )
        self.assertRedirects(response, settings_url)
        saved = CompanySettings.load()
        self.assertEqual(saved.company_name, "Heritage Crops International")
        self.assertEqual(saved.print_layout, CompanySettings.PrintLayout.COMPACT)

        unrelated = User.objects.create_user("settings-outsider", password="test-password")
        self.client.force_login(unrelated)
        self.assertEqual(self.client.get(settings_url).status_code, 403)

    def test_selected_currency_is_used_across_pages(self):
        branding = CompanySettings.load()
        self.assertEqual(branding.currency_symbol, "Rs.")
        branding.currency_code = CompanySettings.Currency.LKR
        branding.save()
        RecurringVoucher.objects.create(
            title="Monthly supplies",
            category=self.category,
            amount=Decimal("125.00"),
            frequency=RecurringVoucher.Frequency.MONTHLY,
            next_run=timezone.localdate(),
            method_of_payment=Voucher.PaymentMethod.BANK_TRANSFER,
            to="Office supplier",
            being="Monthly supplies",
            payee="Office supplier",
            prepared_by=self.preparer,
        )
        self.preparer.groups.add(Group.objects.get(name="Voucher Preparers"))
        finance_group, _ = Group.objects.get_or_create(name="Finance Admins")
        self.preparer.groups.add(finance_group)
        self.voucher.requires_approval = False
        self.voucher.status = Voucher.Status.NO_APPROVAL
        self.voucher.save(update_fields=["requires_approval", "status"])
        self.client.force_login(self.preparer)

        pages = (
            reverse("dashboard"),
            reverse("voucher_list"),
            reverse("voucher_detail", kwargs={"pk": self.voucher.pk}),
            reverse("voucher_print", kwargs={"pk": self.voucher.pk}),
            reverse("reports"),
            reverse("recurring_list"),
        )
        for url in pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "LKR")
                self.assertNotContains(response, "₹")

    def test_pdf_uses_configured_currency(self):
        branding = CompanySettings.load()
        branding.currency_code = CompanySettings.Currency.INR
        branding.save()
        self.voucher.requires_approval = False
        self.voucher.status = Voucher.Status.NO_APPROVAL
        self.voucher.save(update_fields=["requires_approval", "status"])
        self.client.force_login(self.preparer)
        response = self.client.get(
            reverse("voucher_pdf", kwargs={"pk": self.voucher.pk})
        )
        self.assertEqual(response.status_code, 200)
        pdf_stream = re.search(rb"stream\r?\n(.*?)~>endstream", response.content, re.S)
        self.assertIsNotNone(pdf_stream)
        decoded_stream = zlib.decompress(base64.a85decode(pdf_stream.group(1)))
        self.assertIn(b"Rs.", decoded_stream)

    def test_print_view_uses_company_branding(self):
        settings_obj = CompanySettings.load()
        settings_obj.company_name = "Heritage Crops International"
        settings_obj.header_text = "Plantation Division"
        settings_obj.save()
        self.voucher.requires_approval = False
        self.voucher.status = Voucher.Status.NO_APPROVAL
        self.voucher.save(update_fields=["requires_approval", "status"])
        self.client.force_login(self.preparer)
        response = self.client.get(
            reverse("voucher_print", kwargs={"pk": self.voucher.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Heritage Crops International")
        self.assertContains(response, "Plantation Division")
        self.assertContains(response, self.voucher.ref_no)

    def test_pdf_renders_uploaded_custom_logo(self):
        image_buffer = BytesIO()
        PillowImage.new("RGB", (40, 20), color="#254C3B").save(image_buffer, format="PNG")
        upload = SimpleUploadedFile("company-logo.png", image_buffer.getvalue(), "image/png")
        self.client.force_login(self.preparer)
        self.voucher.requires_approval = False
        self.voucher.status = Voucher.Status.NO_APPROVAL
        self.voucher.save(update_fields=["requires_approval", "status"])
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            settings_obj = CompanySettings.load()
            settings_obj.logo.save(upload.name, upload, save=True)
            settings_obj.company_name = "Custom Logo Plantation"
            settings_obj.print_layout = CompanySettings.PrintLayout.PORTRAIT
            settings_obj.save()
            signature_buffer = BytesIO()
            PillowImage.new("RGB", (36, 16), color="#6C9B72").save(
                signature_buffer, format="PNG"
            )
            self.voucher.signature.save(
                "payee-signature.png",
                SimpleUploadedFile(
                    "payee-signature.png", signature_buffer.getvalue(), "image/png"
                ),
                save=True,
            )
            self.voucher.authorized_signature.save(
                "authorized-signature.png",
                SimpleUploadedFile(
                    "authorized-signature.png",
                    signature_buffer.getvalue(),
                    "image/png",
                ),
                save=True,
            )
            response = self.client.get(
                reverse("voucher_pdf", kwargs={"pk": self.voucher.pk})
            )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content.startswith(b"%PDF-"))
            print_response = self.client.get(
                reverse("voucher_print", kwargs={"pk": self.voucher.pk})
            )
            self.assertContains(print_response, "voucher-paper-portrait")
            self.assertContains(print_response, "attachment/signature/")
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            settings_obj = CompanySettings.load()
            settings_obj.logo = ""
            settings_obj.save()

    def test_voucher_is_not_visible_to_unrelated_user(self):
        unrelated = User.objects.create_user("unrelated", password="test-password")
        self.client.force_login(unrelated)
        response = self.client.get(
            reverse("voucher_detail", kwargs={"pk": self.voucher.pk})
        )
        self.assertEqual(response.status_code, 403)

    def test_dashboard_renders_for_authenticated_user(self):
        self.client.force_login(self.preparer)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Voucher dashboard")

    def test_reports_render_budget_progress_without_inline_css(self):
        admin_group, _ = Group.objects.get_or_create(name="Finance Admins")
        self.preparer.groups.add(admin_group)
        self.voucher.status = Voucher.Status.AUTHORIZED
        self.voucher.date = timezone.localdate()
        self.voucher.save(update_fields=["status", "date"])
        self.client.force_login(self.preparer)
        response = self.client.get(reverse("reports"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Monthly category budgets")
        self.assertContains(response, 'class="budget-progress')
        self.assertContains(response, "Categories with budgets")
        self.assertContains(response, "Rs. 250.00")
        self.assertContains(response, "25.0%")
        self.assertNotContains(response, "style=\"width:")


@override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class VoucherApprovalWebTests(TestCase):
    def setUp(self):
        self.creator = User.objects.create_user(
            "voucher-creator", "creator@example.com", "creator-password"
        )
        self.creator.groups.add(Group.objects.get(name="Voucher Preparers"))
        self.approver = User.objects.create_user(
            "assigned-approver", "approver@example.com", "approver-password"
        )
        self.approver.first_name = "Assigned"
        self.approver.last_name = "Approver"
        self.approver.save()
        self.approver.groups.add(Group.objects.get(name="Voucher Approvers"))
        self.inactive_approver = User.objects.create_user(
            "inactive-approver", "inactive@example.com", "inactive-password"
        )
        self.inactive_approver.is_active = False
        self.inactive_approver.save()
        self.inactive_approver.groups.add(Group.objects.get(name="Voucher Approvers"))
        self.other_approver = User.objects.create_user(
            "other-approver", "other@example.com", "other-password"
        )
        self.other_approver.groups.add(Group.objects.get(name="Voucher Approvers"))
        self.unprivileged = User.objects.create_user(
            "unprivileged", "unprivileged@example.com", "unprivileged-password"
        )
        self.category = ExpenseCategory.objects.create(name="Approval test expenses")

    def voucher_data(self, **overrides):
        data = {
            "amount": "125.00",
            "date": "2026-11-02",
            "method_of_payment": Voucher.PaymentMethod.BANK_TRANSFER,
            "to": "Office supplier",
            "being": "Approval workflow test",
            "payee": "Office supplier",
            "category": str(self.category.pk),
            "requires_approval": "on",
            "approver": str(self.approver.pk),
        }
        data.update(overrides)
        return data

    def create_voucher(self, data=None):
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("voucher_create"), data or self.voucher_data()
        )
        self.assertEqual(response.status_code, 302)
        return Voucher.objects.get(prepared_by=self.creator)

    def test_selected_approver_is_eligible_and_owns_the_queue_item(self):
        self.client.force_login(self.creator)
        form_response = self.client.get(reverse("voucher_create"))
        self.assertContains(form_response, "assigned-approver")
        self.assertNotContains(form_response, "inactive-approver")
        self.assertNotContains(form_response, "unprivileged")

        response = self.client.post(
            reverse("voucher_create"), self.voucher_data()
        )
        self.assertEqual(response.status_code, 302)
        voucher = Voucher.objects.get(prepared_by=self.creator)
        self.assertEqual(voucher.status, Voucher.Status.PREPARED)
        self.assertEqual(voucher.approver, self.approver)
        self.assertIsNotNone(voucher.approval_requested_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.approver.email])

        for route in ("voucher_print", "voucher_pdf"):
            pending_response = self.client.get(
                reverse(route, kwargs={"pk": voucher.pk})
            )
            self.assertEqual(pending_response.status_code, 403)
        self.assertContains(
            self.client.get(reverse("voucher_detail", kwargs={"pk": voucher.pk})),
            "Print unavailable until approved",
        )

        self.client.force_login(self.other_approver)
        queue = self.client.get(reverse("approval_queue"))
        self.assertNotContains(queue, voucher.ref_no)
        self.assertEqual(
            self.client.get(
                reverse("voucher_detail", kwargs={"pk": voucher.pk})
            ).status_code,
            403,
        )

        self.client.force_login(self.approver)
        queue = self.client.get(reverse("approval_queue"))
        self.assertContains(queue, voucher.ref_no)
        response = self.client.post(
            reverse(
                "voucher_action",
                kwargs={"pk": voucher.pk, "action": "approve"},
            )
        )
        self.assertEqual(response.status_code, 302)
        voucher.refresh_from_db()
        self.assertEqual(voucher.status, Voucher.Status.APPROVED)
        self.assertEqual(voucher.approved_by, self.approver)
        self.assertIsNotNone(voucher.approved_at)
        self.assertEqual(voucher.audit_logs.count(), 3)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[1].to, [self.creator.email])
        self.assertEqual(
            self.client.get(
                reverse("voucher_print", kwargs={"pk": voucher.pk})
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse("voucher_pdf", kwargs={"pk": voucher.pk})
            ).status_code,
            200,
        )

    def test_no_approval_workflow_is_immediately_printable(self):
        voucher = self.create_voucher(
            self.voucher_data(requires_approval="", approver="")
        )
        self.assertFalse(voucher.requires_approval)
        self.assertIsNone(voucher.approver)
        self.assertEqual(voucher.status, Voucher.Status.NO_APPROVAL)
        self.assertTrue(voucher.is_printable)
        self.assertEqual(
            self.client.get(
                reverse("voucher_print", kwargs={"pk": voucher.pk})
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                reverse("voucher_pdf", kwargs={"pk": voucher.pk})
            ).status_code,
            200,
        )

    def test_rejection_requires_selected_approver_and_records_reason_and_time(self):
        voucher = self.create_voucher()
        self.client.force_login(self.other_approver)
        self.assertEqual(
            self.client.post(
                reverse(
                    "voucher_action",
                    kwargs={"pk": voucher.pk, "action": "reject"},
                ),
                {"reason": "Not approved"},
            ).status_code,
            403,
        )
        self.client.force_login(self.approver)
        response = self.client.post(
            reverse("voucher_action", kwargs={"pk": voucher.pk, "action": "reject"}),
            {"reason": "Missing supporting receipt"},
        )
        self.assertEqual(response.status_code, 302)
        voucher.refresh_from_db()
        self.assertEqual(voucher.status, Voucher.Status.REJECTED)
        self.assertEqual(voucher.rejection_reason, "Missing supporting receipt")
        self.assertIsNotNone(voucher.rejected_at)
        self.assertEqual(voucher.audit_logs.first().notes, "Missing supporting receipt")

    def test_status_is_not_exposed_in_form_or_editable_by_action_url(self):
        voucher = self.create_voucher()
        from .forms import VoucherForm

        self.assertNotIn("status", VoucherForm().fields)
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse(
                "voucher_action",
                kwargs={"pk": voucher.pk, "action": "authorize"},
            )
        )
        self.assertEqual(response.status_code, 403)
        voucher.refresh_from_db()
        self.assertEqual(voucher.status, Voucher.Status.PREPARED)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RecurringVoucherTests(TestCase):
    def test_due_schedule_is_generated_automatically_during_application_request(self):
        preparer = User.objects.create_user(
            "automatic-preparer", "automatic@example.com", "automatic-password"
        )
        preparer.groups.add(Group.objects.get(name="Voucher Preparers"))
        category = ExpenseCategory.objects.create(name="Automatic recurring")
        schedule = RecurringVoucher.objects.create(
            title="Automatic rent",
            category=category,
            amount=Decimal("500.00"),
            frequency=RecurringVoucher.Frequency.MONTHLY,
            next_run=timezone.localdate(),
            method_of_payment=Voucher.PaymentMethod.BANK_TRANSFER,
            to="Landlord",
            being="Automatic rent",
            payee="Landlord",
            prepared_by=preparer,
        )
        cache.clear()
        self.client.force_login(preparer)

        response = self.client.get(reverse("recurring_list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(schedule.generated_vouchers.count(), 1)
        schedule.refresh_from_db()
        self.assertEqual(
            schedule.next_run,
            advance_date(timezone.localdate(), RecurringVoucher.Frequency.MONTHLY),
        )

    def test_calendar_month_advance_clamps_to_last_day(self):
        self.assertEqual(advance_date(date(2026, 1, 31), "monthly"), date(2026, 2, 28))
        self.assertEqual(advance_date(date(2026, 11, 2), "weekly"), date(2026, 11, 9))

    def test_generation_is_idempotent(self):
        from django.utils import timezone

        preparer = User.objects.create_user("recurring-preparer", password="test-password")
        category = ExpenseCategory.objects.create(name="Rent")
        due_date = timezone.localdate().replace(day=1)
        schedule = RecurringVoucher.objects.create(
            title="Monthly rent",
            category=category,
            amount=Decimal("500.00"),
            frequency=RecurringVoucher.Frequency.MONTHLY,
            next_run=due_date,
            method_of_payment=Voucher.PaymentMethod.BANK_TRANSFER,
            to="Landlord",
            being="Monthly rent",
            payee="Landlord",
            prepared_by=preparer,
        )
        call_command("generate_recurring")
        call_command("generate_recurring")
        self.assertEqual(schedule.generated_vouchers.count(), 1)
        schedule.refresh_from_db()
        self.assertEqual(schedule.next_run, advance_date(due_date, "monthly"))

    def test_creator_with_approval_permission_can_save_self_approved_schedule(self):
        creator = User.objects.create_user(
            "recurring-approver", "recurring@example.com", "recurring-password"
        )
        creator.groups.add(
            Group.objects.get(name="Voucher Preparers"),
            Group.objects.get(name="Voucher Approvers"),
        )
        category = ExpenseCategory.objects.create(name="Self approved recurring")
        form = RecurringVoucherForm(
            data={
                "title": "Monthly supplies",
                "category": category.pk,
                "amount": "125.00",
                "frequency": RecurringVoucher.Frequency.MONTHLY,
                "next_run": "2026-11-03",
                "method_of_payment": Voucher.PaymentMethod.BANK_TRANSFER,
                "to": "Supplier",
                "being": "Monthly supplies",
                "payee": "Supplier",
                "requires_approval": "on",
                "approver": creator.pk,
                "is_active": "on",
            },
            user=creator,
        )
        self.assertTrue(form.is_valid(), form.errors)
        schedule = form.save(commit=False)
        schedule.prepared_by = creator
        schedule.save()
        self.assertEqual(schedule.approver, creator)

        call_command("generate_recurring")
        voucher = schedule.generated_vouchers.get()
        transition_voucher(voucher.pk, creator, "submit")
        transition_voucher(voucher.pk, creator, "approve")
        voucher.refresh_from_db()
        self.assertEqual(voucher.status, Voucher.Status.APPROVED)
        self.assertEqual(voucher.approved_by, creator)

    def test_creator_without_approval_permission_cannot_select_self_as_approver(self):
        creator = User.objects.create_user(
            "recurring-preparer-only", "preparer@example.com", "preparer-password"
        )
        creator.groups.add(Group.objects.get(name="Voucher Preparers"))
        category = ExpenseCategory.objects.create(name="Restricted recurring")
        form = RecurringVoucherForm(
            data={
                "title": "Restricted schedule",
                "category": category.pk,
                "amount": "125.00",
                "frequency": RecurringVoucher.Frequency.MONTHLY,
                "next_run": "2026-11-03",
                "method_of_payment": Voucher.PaymentMethod.BANK_TRANSFER,
                "to": "Supplier",
                "being": "Restricted schedule",
                "payee": "Supplier",
                "requires_approval": "on",
                "approver": creator.pk,
                "is_active": "on",
            },
            user=creator,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("approver", form.errors)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class UserManagementTests(TestCase):
    def setUp(self):
        settings_obj = CompanySettings.load()
        settings_obj.branches_enabled = True
        settings_obj.save()
        self.admin = User.objects.create_superuser(
            "user-admin", "admin@example.com", "admin-password"
        )
        self.branch = Branch.objects.create(name="North Office", code="NORTH")

    def test_superuser_can_create_user_with_role_branch_and_password(self):
        self.client.force_login(self.admin)
        role = Group.objects.get(name="Voucher Preparers")
        response = self.client.post(
            reverse("user_create"),
            {
                "username": "branch-preparer",
                "first_name": "Branch",
                "last_name": "Preparer",
                "email": "preparer@example.com",
                "is_active": "on",
                "password1": "Strong-test-password-382!",
                "password2": "Strong-test-password-382!",
                "role": role.pk,
                "branch": self.branch.pk,
            },
        )
        self.assertRedirects(response, reverse("user_list"))
        user = User.objects.get(username="branch-preparer")
        self.assertTrue(user.check_password("Strong-test-password-382!"))
        self.assertEqual(user.groups.get(), role)
        self.assertEqual(user.voucher_profile.branch, self.branch)
        self.assertEqual(user.voucher_profile.role_group, role)

    def test_superuser_can_edit_group_permissions(self):
        permission = Permission.objects.get(
            content_type__app_label="auth", codename="view_user"
        )
        form = GroupManagementForm(
            data={"name": "Voucher Auditors", "permissions": [permission.pk]},
            actor=self.admin,
        )
        self.assertTrue(form.is_valid(), form.errors)
        group = form.save()
        self.assertEqual(list(group.permissions.all()), [permission])

    def test_management_pages_render_for_authorized_superuser(self):
        self.client.force_login(self.admin)
        for route in ("user_list", "user_create", "group_list", "group_create"):
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)


    def test_non_superuser_cannot_create_workflow_role_group(self):
        manager = User.objects.create_user("group-manager", password="manager-pass")
        form = GroupManagementForm(
            data={"name": "Finance Admins", "permissions": []},
            actor=manager,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_branch_user_cannot_view_or_edit_another_branch_voucher(self):
        other_branch = Branch.objects.create(name="South Office", code="SOUTH")
        preparer = User.objects.create_user("north-preparer", password="secret")
        UserProfile.objects.filter(user=preparer).update(branch=self.branch)
        preparer.groups.add(Group.objects.get(name="Voucher Preparers"))
        category = ExpenseCategory.objects.create(name="Branch expenses")
        voucher = Voucher.objects.create(
            amount=Decimal("25.00"),
            method_of_payment=Voucher.PaymentMethod.CASH,
            to="Supplier",
            being="Office supplies",
            payee="Supplier",
            category=category,
            prepared_by=preparer,
            branch=other_branch,
        )
        self.client.force_login(preparer)
        self.assertEqual(
            self.client.get(reverse("voucher_detail", kwargs={"pk": voucher.pk})).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("voucher_update", kwargs={"pk": voucher.pk})).status_code,
            403,
        )

    def test_branch_filter_and_cross_branch_permission(self):
        other_branch = Branch.objects.create(name="South Office", code="SOUTH")
        preparer = User.objects.create_user("north-preparer", password="secret")
        UserProfile.objects.filter(user=preparer).update(branch=self.branch)
        preparer.groups.add(Group.objects.get(name="Voucher Recommenders"))
        category = ExpenseCategory.objects.create(name="Branch expenses")
        local_voucher = Voucher.objects.create(
            amount=Decimal("25.00"),
            method_of_payment=Voucher.PaymentMethod.CASH,
            to="Local supplier",
            being="North office expense",
            payee="Local supplier",
            category=category,
            prepared_by=self.admin,
            branch=self.branch,
            status=Voucher.Status.PREPARED,
        )
        foreign_voucher = Voucher.objects.create(
            amount=Decimal("30.00"),
            method_of_payment=Voucher.PaymentMethod.CASH,
            to="South supplier",
            being="South office expense",
            payee="South supplier",
            category=category,
            prepared_by=self.admin,
            branch=other_branch,
            status=Voucher.Status.PREPARED,
        )
        self.client.force_login(preparer)
        response = self.client.get(reverse("voucher_list"))
        self.assertContains(response, local_voucher.ref_no)
        self.assertNotContains(response, foreign_voucher.ref_no)

        permission = Permission.objects.get(
            content_type__app_label="vouchers", codename="view_all_branches"
        )
        preparer.user_permissions.add(permission)
        response = self.client.get(reverse("voucher_list"))
        self.assertContains(response, foreign_voucher.ref_no)

    def test_recurring_toggle_is_scoped_to_user_branch(self):
        other_branch = Branch.objects.create(name="South Office", code="SOUTH")
        preparer = User.objects.create_user("north-preparer", password="secret")
        UserProfile.objects.filter(user=preparer).update(branch=self.branch)
        preparer.groups.add(Group.objects.get(name="Voucher Preparers"))
        category = ExpenseCategory.objects.create(name="Recurring expenses")
        schedule = RecurringVoucher.objects.create(
            title="South office rent",
            category=category,
            amount=Decimal("100.00"),
            frequency=RecurringVoucher.Frequency.MONTHLY,
            next_run=timezone.localdate(),
            method_of_payment=Voucher.PaymentMethod.BANK_TRANSFER,
            to="Landlord",
            being="Rent",
            payee="Landlord",
            prepared_by=preparer,
            branch=other_branch,
        )
        self.client.force_login(preparer)
        response = self.client.post(
            reverse("recurring_toggle", kwargs={"pk": schedule.pk})
        )
        self.assertEqual(response.status_code, 404)
        schedule.refresh_from_db()
        self.assertTrue(schedule.is_active)
