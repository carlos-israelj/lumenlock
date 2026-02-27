# Generated migration to fix secret_seed field length
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wallet', '0002_remove_wallet_balance'),
    ]

    operations = [
        migrations.AlterField(
            model_name='wallet',
            name='secret_seed',
            field=models.CharField(max_length=200),
        ),
    ]
