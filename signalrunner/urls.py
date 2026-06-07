"""signalrunner/urls.py — app-level URL patterns."""
from django.urls import path
from signalrunner import views

urlpatterns = [
    # Dashboard
    path("", views.dashboard, name="dashboard"),

    # Strategies
    path("strategies/", views.strategy_list, name="strategy_list"),
    path("strategies/new/", views.strategy_new, name="strategy_new"),
    path("strategies/<uuid:strategy_id>/", views.strategy_detail, name="strategy_detail"),
    path("strategies/<uuid:strategy_id>/edit/", views.strategy_edit, name="strategy_edit"),
    path("strategies/<uuid:strategy_id>/evaluate/", views.strategy_evaluate, name="strategy_evaluate"),
    path("strategies/<uuid:strategy_id>/toggle/", views.strategy_toggle, name="strategy_toggle"),

    # Evaluations
    path("evaluations/", views.evaluation_list, name="evaluation_list"),
    path("evaluations/<uuid:eval_id>/", views.evaluation_detail, name="evaluation_detail"),

    # Signals
    path("signals/", views.signal_list, name="signal_list"),

    # Backtesting
    path("strategies/<uuid:strategy_id>/backtest/", views.backtest_new, name="backtest_new"),
    path("backtests/", views.backtest_list, name="backtest_list"),
    path("backtests/<uuid:backtest_id>/", views.backtest_detail, name="backtest_detail"),

    # Fundamentals
    path("fundamentals/", views.fundamentals, name="fundamentals"),
    path("fundamentals/refresh/", views.fundamentals_refresh, name="fundamentals_refresh"),

    # API
    path("api/strategies/<uuid:strategy_id>/evaluate", views.api_evaluate, name="api_evaluate"),
    path("api/evaluations/<uuid:eval_id>", views.api_evaluation_status, name="api_evaluation_status"),
]
