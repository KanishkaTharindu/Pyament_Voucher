from django.db import migrations


SAMPLE_CATEGORIES = (
    ("Plantation Operations", "Plantation, field, and crop production expenses."),
    ("Office Supplies", "Office stationery and administrative supplies."),
    ("Travel and Conveyance", "Business travel, transport, and conveyance."),
    ("Utilities", "Electricity, water, telephone, and internet."),
    ("Repairs and Maintenance", "Building, equipment, and vehicle maintenance."),
    ("Staff Welfare", "Staff welfare and employee-related expenses."),
    ("Export and Freight", "Export handling, freight, and logistics."),
    ("Professional Fees", "Legal, accounting, consulting, and other professional fees."),
    ("General Expenses", "Other approved business expenses."),
)


def create_sample_categories(apps, schema_editor):
    ExpenseCategory = apps.get_model("vouchers", "ExpenseCategory")
    ExpenseCategory.objects.using(schema_editor.connection.alias).bulk_create(
        [
            ExpenseCategory(name=name, description=description)
            for name, description in SAMPLE_CATEGORIES
        ],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [("vouchers", "0001_initial")]

    operations = [
        migrations.RunPython(create_sample_categories, migrations.RunPython.noop),
    ]
