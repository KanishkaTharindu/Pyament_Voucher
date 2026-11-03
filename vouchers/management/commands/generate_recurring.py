from django.core.management.base import BaseCommand
from vouchers.services import generate_due_recurring


class Command(BaseCommand):
    help = "Generate draft vouchers for recurring schedules that are due."

    def handle(self, *args, **options):
        generated = generate_due_recurring()
        self.stdout.write(self.style.SUCCESS(f"Generated {generated} draft voucher(s)."))
