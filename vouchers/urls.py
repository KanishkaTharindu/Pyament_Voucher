from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("vouchers/", views.voucher_list, name="voucher_list"),
    path("vouchers/new/", views.voucher_create, name="voucher_create"),
    path("vouchers/approvers/", views.approver_options, name="approver_options"),
    path("vouchers/<int:pk>/", views.voucher_detail, name="voucher_detail"),
    path("vouchers/<int:pk>/edit/", views.voucher_update, name="voucher_update"),
    path("vouchers/<int:pk>/pdf/", views.voucher_pdf, name="voucher_pdf"),
    path("vouchers/<int:pk>/print/", views.voucher_print, name="voucher_print"),
    path(
        "vouchers/<int:pk>/attachment/<str:kind>/",
        views.voucher_attachment,
        name="voucher_attachment",
    ),
    path("vouchers/<int:pk>/action/<str:action>/", views.voucher_action, name="voucher_action"),
    path("reports/", views.reports, name="reports"),
    path("approvals/", views.approval_queue, name="approval_queue"),
    path("company-settings/", views.company_settings, name="company_settings"),
    path("company-logo/", views.company_logo, name="company_logo"),
    path("branches/", views.branch_list, name="branch_list"),
    path("branches/new/", views.branch_create, name="branch_create"),
    path("branches/<int:pk>/edit/", views.branch_update, name="branch_update"),
    path("branches/<int:pk>/toggle/", views.branch_toggle, name="branch_toggle"),
    path("recurring/", views.recurring_list, name="recurring_list"),
    path("recurring/new/", views.recurring_create, name="recurring_create"),
    path("recurring/<int:pk>/toggle/", views.recurring_toggle, name="recurring_toggle"),
    path("users/", views.user_list, name="user_list"),
    path("users/new/", views.user_create, name="user_create"),
    path("users/<int:pk>/edit/", views.user_update, name="user_update"),
    path("groups/", views.group_list, name="group_list"),
    path("groups/new/", views.group_create, name="group_create"),
    path("groups/<int:pk>/edit/", views.group_update, name="group_update"),
]
