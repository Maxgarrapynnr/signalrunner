from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("signalrunner", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name='Backtest',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('strategy_name', models.CharField(blank=True, max_length=200)),
                ('config_snapshot', models.JSONField(blank=True, default=dict)),
                ('start_date', models.DateField()),
                ('end_date', models.DateField()),
                ('horizon_days', models.PositiveIntegerField(default=5)),
                ('take_profit_pct', models.FloatField(blank=True, null=True)),
                ('stop_loss_pct', models.FloatField(blank=True, null=True)),
                ('status', models.CharField(choices=[('queued', 'Queued'), ('running', 'Running'), ('success', 'Success'), ('failed', 'Failed')], default='queued', max_length=12)),
                ('stats', models.JSONField(blank=True, default=dict)),
                ('equity_curve', models.JSONField(blank=True, default=list)),
                ('log', models.JSONField(blank=True, default=list)),
                ('error', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('duration_ms', models.PositiveIntegerField(blank=True, null=True)),
                ('strategy', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='backtests', to='signalrunner.strategy')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='BacktestSignal',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('ticker', models.CharField(max_length=20)),
                ('direction', models.CharField(choices=[('buy', 'Buy'), ('sell', 'Sell')], max_length=4)),
                ('session_date', models.DateField()),
                ('entry_price', models.FloatField()),
                ('exit_price', models.FloatField(blank=True, null=True)),
                ('exit_date', models.DateField(blank=True, null=True)),
                ('return_pct', models.FloatField(blank=True, null=True)),
                ('won', models.BooleanField(null=True)),
                ('exit_kind', models.CharField(blank=True, max_length=12)),
                ('reason', models.JSONField(blank=True, default=dict)),
                ('backtest', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='signals', to='signalrunner.backtest')),
            ],
            options={
                'ordering': ['session_date'],
                'indexes': [models.Index(fields=['backtest', 'session_date'], name='signalrunne_backtes_5490fa_idx')],
            },
        ),
    ]
