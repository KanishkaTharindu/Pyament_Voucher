from .models import CompanySettings


def company_settings(request):
    return {"company_settings": CompanySettings.load()}
