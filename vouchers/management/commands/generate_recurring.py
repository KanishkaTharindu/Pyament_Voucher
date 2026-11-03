from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from vouchers.models import AuditLog, RecurringVoucher, Voucher
from vouchers.services import advance_date


class Command(BaseCommand):
    help = "Generate draft vouchers for recurring schedules that are due."

    def handle(self, *args, **options):
        today = timezone.localdate()
        generated = 0
        for recurring in RecurringVoucher.objects.filter(
            is_active=True, next_run__lte=today
        ).select_related("category", "prepared_by"):
            with transaction.atomic():
                recurring = RecurringVoucher.objects.select_for_update().get(pk=recurring.pk)
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
        self.stdout.write(self.style.SUCCESS(f"Generated {generated} draft voucher(s)."))
