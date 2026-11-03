# Heritage Crops Payment Voucher Management

A Django application for preparing, reviewing, approving, authorizing, searching, and reporting on office payment vouchers for **Heritage Crops Plantation and Exports Pvt Ltd**. The voucher PDF reflects the fields and approval/signature areas of the supplied paper voucher.

## Features

- Voucher reference numbers, amount/date, cash and payment method, payee, account details, cheque number, receipt, purpose, category, and signature uploads.
- Two voucher paths: create without approval and print immediately, or submit to one selected approver and print only after approval. Active approvers are selected from users with `vouchers.approve_voucher`; the assigned approver or a user with `vouchers.approve_any_voucher` can decide the request.
- Approval/rejection timestamps, required rejection reasons, an audit trail, and email notifications to the selected approver and voucher creator. Existing vouchers in the older Prepared → Recommended → Approved → Authorized group workflow remain supported.
- Role-based Django permissions for voucher preparation, assigned approvals, finance administration, branch management, and cross-branch access. Users cannot approve their own vouchers.
- Monthly expense-category budgets checked before approval/authorization.
- Server-side checks protect print and PDF endpoints as well as the buttons in the interface; pending or rejected approval-required vouchers cannot be printed by directly opening a URL.
- Search and filters, dashboard, monthly spend and budget reports, and printable/downloadable PDF vouchers.
- Dedicated **Company settings** page for finance administrators to customize the company name, logo, header/footer text, Forest/Ocean/Clay/Slate/custom colors, and landscape/portrait/compact print layouts. Settings are applied to the application, browser print view, and generated PDFs.
- Configurable currency across dashboards, reports, voucher and recurring lists, voucher details, browser printing, and PDFs. Indian Rupee (**Rs.**) is the default; INR, LKR, USD, EUR, GBP, AED, SGD, BDT, and NPR are available.
- Recurring voucher schedules that create draft vouchers using a management command.
- Optional branch management from Company settings, including create/edit/activate/deactivate actions, branch-scoped voucher numbering, approver eligibility, access, and reporting. Branches are disabled by default; existing voucher/branch history is retained when the feature is disabled or a branch is deactivated.
- Branch-aware user administration: add and edit users, assign workflow roles and branches, set direct Django permissions, and manage reusable groups with permission checkboxes. When branches are enabled, branch-assigned users see records for their branch; Finance Admins and users granted cross-branch access can view all branches.
- Django admin for users, groups, categories, vouchers, recurring schedules, and audit history.
- Receipt uploads accept PDF/JPG/PNG; signature uploads accept JPG/PNG. Upload limit: 10 MB per file.
- Receipt and signature downloads require an authenticated user who is allowed to view the voucher; uploaded files are not publicly served.

## Requirements

- Python 3.10 or later
- pip

## Run locally on Windows

Open PowerShell in this project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/` and sign in with the superuser. The Django admin is at `http://127.0.0.1:8000/admin/`.

If PowerShell prevents environment activation, either allow it for the current process with `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, or run `.venv\Scripts\python.exe` instead of activating the environment.

## First-time setup

1. Sign in to `/admin/` as the superuser.
2. Open **Company settings** from the navigation bar to customize the brand and printed voucher. Finance administrators and superusers can update these settings; the Django admin also exposes the settings record.
3. Starter expense categories are added automatically by migrations: Plantation Operations, Office Supplies, Travel and Conveyance, Utilities, Repairs and Maintenance, Staff Welfare, Export and Freight, Professional Fees, and General Expenses. Edit these categories or add more in `/admin/` and set monthly budget limits as needed. A limit of zero means unlimited.
4. If branch operation is required, open **Company settings**, enable branch functionality, and use **Manage branches** to create and activate branches. Assign users to active branches from **Users** in the application navigation. Deactivating a branch prevents new assignments but retains its historical vouchers.
5. Use **Users** in the application navigation to create and maintain accounts, choose a primary workflow role, and set direct permissions. The **Manage groups** page lets authorized administrators manage reusable Django permission groups. Superusers can assign any role or permission; non-superusers can assign only roles and groups they are authorized to manage.
6. The built-in workflow groups are:
   - **Voucher Preparers**: create and submit vouchers; manage recurring schedules.
   - **Voucher Approvers**: receive `vouchers.approve_voucher` by default and can be selected to approve a voucher.
   - **Finance Admins**: receive `vouchers.approve_any_voucher` and branch/report permissions by default.
   - **Voucher Recommenders** and **Voucher Authorizers**: retain the legacy multi-stage workflow for existing vouchers.
7. Add email addresses to approvers and preparers if notifications are required. A superuser can override assigned approvals, but the separation-of-duties rule still applies.

When branches are disabled, voucher creation does not require a branch and the application uses organization-wide behavior. When enabled, users with no branch assignment do not receive branch-scoped records; assign branches to location-scoped users. Grant `vouchers.view_all_branches` when a non-Finance-Admin needs cross-branch visibility.

Creating a voucher asks whether approval is required. Approval-required vouchers are submitted to the selected approver as part of creation and appear in that person's **Approvals** queue. The selected approver (or a user with `vouchers.approve_any_voucher`) can approve or reject it. Vouchers marked as not requiring approval become printable immediately. Recurring schedules generate drafts, which must be submitted separately.

## Recurring vouchers

Create a schedule from **Recurring**. Due schedules produce draft vouchers; they do not bypass the approval process. Run the generator manually:

```powershell
python manage.py generate_recurring
```

For automatic generation, schedule that command with Windows Task Scheduler (for example, once each morning), using the project directory as the working directory and `.venv\Scripts\python.exe` as the program. Generation is idempotent for each schedule and due date.

## Email configuration

The local default is Django's console email backend. To use SMTP, set environment variables before starting the application:

```powershell
$env:DJANGO_EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
$env:EMAIL_HOST = "smtp.example.com"
$env:EMAIL_PORT = "587"
$env:EMAIL_HOST_USER = "your-smtp-user"
$env:EMAIL_HOST_PASSWORD = "your-smtp-password"
$env:EMAIL_USE_TLS = "1"
```

For a real deployment, configure SMTP credentials through a secret manager and add corresponding settings for the selected mail provider.

## Useful commands

```powershell
python manage.py check
python manage.py test
python manage.py createsuperuser
python manage.py generate_recurring
```

The application uses SQLite for local development and creates `db.sqlite3` on migration. Before production use, set a private `DJANGO_SECRET_KEY`, turn off `DJANGO_DEBUG`, configure `DJANGO_ALLOWED_HOSTS`, use a production database and HTTPS, and configure private persistent storage for uploaded receipts and signatures. Do not expose Django's development server or debug media serving to the public internet.
