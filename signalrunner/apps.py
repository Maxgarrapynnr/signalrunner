"""signalrunner/apps.py"""
from django.apps import AppConfig


class SignalrunnerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "signalrunner"

    def ready(self):
        """Register recurring django-q schedules on startup."""
        from django.db import connection
        try:
            # Only register schedules if the DB tables actually exist
            if "django_q_schedule" in connection.introspection.table_names():
                _register_schedules()
        except Exception:
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
        {
            "name": "Daily Fundamentals Refresh",
            "func": "signalrunner.fundamentals.refresh_all",
            "schedule_type": Schedule.DAILY,
            "minutes": 0,
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
