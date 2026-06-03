"""signalrunner/admin.py"""
from django import forms
from django.contrib import admin

from signalrunner.models import (
    Strategy, Evaluation, Signal, MarketDataSnapshot, Delivery, Secret,
)


class SecretForm(forms.ModelForm):
    new_value = forms.CharField(
        required=False, widget=forms.PasswordInput(render_value=False),
        help_text="Enter to set/replace. Leave blank to keep current.",
    )
    class Meta:
        model = Secret
        fields = ["name", "description"]

    def save(self, commit=True):
        secret = super().save(commit=False)
        plaintext = self.cleaned_data.get("new_value")
        if plaintext:
            secret.set_value(plaintext)
        if commit:
            secret.save()
        return secret


@admin.register(Secret)
class SecretAdmin(admin.ModelAdmin):
    form = SecretForm
    list_display = ["name", "description", "updated_at"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(Strategy)
class StrategyAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "enabled", "schedule_kind", "created_at"]
    list_filter = ["kind", "enabled", "schedule_kind"]
    search_fields = ["name"]


class SignalInline(admin.TabularInline):
    model = Signal
    extra = 0
    readonly_fields = ["ticker", "direction", "price", "reason", "created_at"]
    can_delete = False


@admin.register(Evaluation)
class EvaluationAdmin(admin.ModelAdmin):
    list_display = ["id", "strategy", "status", "fired", "trigger", "queued_at"]
    list_filter = ["status", "fired", "trigger"]
    readonly_fields = ["strategy", "trigger", "status", "fired", "computed",
                       "log", "error", "queued_at", "started_at", "finished_at", "duration_ms"]
    inlines = [SignalInline]


@admin.register(Signal)
class SignalAdmin(admin.ModelAdmin):
    list_display = ["ticker", "direction", "price", "strategy", "created_at"]
    list_filter = ["direction", "ticker"]


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ["id", "kind", "status", "attempts", "created_at"]
    list_filter = ["status", "kind"]
    readonly_fields = ["signal", "kind", "target", "attempts", "last_error",
                       "created_at", "sent_at"]


@admin.register(MarketDataSnapshot)
class MarketDataSnapshotAdmin(admin.ModelAdmin):
    list_display = ["ticker", "price", "pct_change", "source", "fetched_at"]
    list_filter = ["ticker", "source"]
    readonly_fields = ["ticker", "price", "pct_change", "volume", "raw",
                       "source", "fetched_at"]
