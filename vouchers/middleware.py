import logging

from django.core.cache import cache
from django.db import IntegrityError, OperationalError

from .services import generate_due_recurring

logger = logging.getLogger(__name__)


class RecurringVoucherMiddleware:
    """Generate due schedules opportunistically without a separate worker."""

    cache_key = "vouchers:recurring:last-automatic-run"
    interval_seconds = 30

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if cache.add(self.cache_key, True, self.interval_seconds):
            try:
                generate_due_recurring()
            except (IntegrityError, OperationalError):
                logger.exception("Automatic recurring voucher generation failed.")
        return self.get_response(request)
