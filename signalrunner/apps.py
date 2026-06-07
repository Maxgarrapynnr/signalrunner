"""signalrunner/apps.py"""
from django.apps import AppConfig


class SignalrunnerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "signalrunner"

    def ready(self):
        """Register recurring django-q schedules on startup."""
        try:
            _register_schedules()
        except Exception:
            # DB may not be ready yet (e.g. during migrate); skip silently.
            pass


def _register_schedules():
    """Ensure the announcement monitor and Telegram bot poller are scheduled."""
    from django_q.models import Schedule

    schedules = [
        {
            "name": "BVC Announcement Monitor",
            "func": "signalrunner.announcement_monitor.check_announcements",
            "schedule_type": Schedule.MINUTES,
            "minutes": 15,
        },
        {
            "name": "Telegram Command Poller",
            "func": "signalrunner.telegram_bot.poll_telegram_commands",
            "schedule_type": Schedule.MINUTES,
            "minutes": 1,
        },
    ]

    for s in schedules:
        Schedule.objects.get_or_create(
            name=s["name"],
            defaults={
                "func": s["func"],
                "schedule_type": s["schedule_type"],
                "minutes": s.get("minutes", 1),
                "repeats": -1,  # run forever
            },
        )
