"""
signalrunner/views.py

Full implementation comes in Phase 5 (UI). These stubs let the URL conf load
and the system check pass without import errors.
"""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from signalrunner.models import (
    Evaluation, Signal, Strategy,
    EvaluationStatus, TriggerType, StrategyKind,
)


@login_required
def dashboard(request):
    from django.utils import timezone
    today = timezone.now().date()
    return render(request, "signalrunner/dashboard.html", {
        "recent_signals": Signal.objects.select_related("strategy").all()[:10],
        "recent_evals": Evaluation.objects.select_related("strategy").all()[:10],
        "strategy_count": Strategy.objects.filter(enabled=True).count(),
        "buy_count": Signal.objects.filter(direction="buy", created_at__date=today).count(),
        "sell_count": Signal.objects.filter(direction="sell", created_at__date=today).count(),
    })


@login_required
def strategy_list(request):
    return render(request, "signalrunner/strategy_list.html", {
        "strategies": Strategy.objects.all(),
    })


_STARTER_CODE = (
    "# `quotes` is a dict: {ticker: {price, pct_change, volume}}\n"
    "# Append to `signals`: [{'ticker':..., 'direction':'buy'/'sell', 'reason':{...}}]\n\n"
    "for ticker, q in quotes.items():\n"
    "    if q.get('pct_change', 0) > 5:\n"
    "        signals.append({'ticker': ticker, 'direction': 'buy',\n"
    "                        'reason': {'pct': q['pct_change']}})\n"
)


@login_required
@require_http_methods(["GET", "POST"])
def strategy_new(request):
    if request.method == "GET":
        return render(request, "signalrunner/strategy_form.html", {"starter_code": _STARTER_CODE})
    return _save_strategy(request, None)


@login_required
def strategy_detail(request, strategy_id):
    strategy = get_object_or_404(Strategy, id=strategy_id)
    has_running = strategy.evaluations.filter(
        status__in=[EvaluationStatus.QUEUED, EvaluationStatus.RUNNING]
    ).exists()
    return render(request, "signalrunner/strategy_detail.html", {
        "strategy": strategy,
        "evaluations": strategy.evaluations.all()[:20],
        "signals": strategy.signals.all()[:20],
        "has_running": has_running,
    })


@login_required
@require_http_methods(["GET", "POST"])
def strategy_edit(request, strategy_id):
    strategy = get_object_or_404(Strategy, id=strategy_id)
    if request.method == "GET":
        return render(request, "signalrunner/strategy_form.html",
                      {"strategy": strategy, "starter_code": _STARTER_CODE})
    return _save_strategy(request, strategy)


@login_required
@require_http_methods(["POST"])
def strategy_evaluate(request, strategy_id):
    """Kick off an on-demand evaluation via the worker."""
    from django_q.tasks import async_task
    strategy = get_object_or_404(Strategy, id=strategy_id)
    ev = Evaluation.objects.create(
        strategy=strategy,
        trigger=TriggerType.ON_DEMAND,
        status=EvaluationStatus.QUEUED,
    )
    async_task("signalrunner.tasks.run_evaluation", str(ev.id))
    return redirect("evaluation_detail", eval_id=ev.id)


@login_required
@require_http_methods(["POST"])
def strategy_toggle(request, strategy_id):
    strategy = get_object_or_404(Strategy, id=strategy_id)
    strategy.enabled = not strategy.enabled
    strategy.save(update_fields=["enabled"])
    return redirect("strategy_detail", strategy_id=strategy_id)


@login_required
def evaluation_list(request):
    return render(request, "signalrunner/evaluation_list.html", {
        "evaluations": Evaluation.objects.select_related("strategy").all()[:100],
    })


@login_required
def evaluation_detail(request, eval_id):
    ev = get_object_or_404(Evaluation.objects.select_related("strategy"), id=eval_id)
    if _wants_json(request):
        return JsonResponse(_eval_json(ev))
    return render(request, "signalrunner/evaluation_detail.html", {
        "ev": ev,
        "signals": ev.signals.all(),
    })


@login_required
def signal_list(request):
    return render(request, "signalrunner/signal_list.html", {
        "signals": Signal.objects.select_related("strategy").all()[:200],
    })


# ── JSON API ──────────────────────────────────────────────────────────────────
@login_required
@require_http_methods(["POST"])
def api_evaluate(request, strategy_id):
    from django_q.tasks import async_task
    strategy = get_object_or_404(Strategy, id=strategy_id)
    ev = Evaluation.objects.create(
        strategy=strategy,
        trigger=TriggerType.ON_DEMAND,
        status=EvaluationStatus.QUEUED,
    )
    async_task("signalrunner.tasks.run_evaluation", str(ev.id))
    return JsonResponse({"eval_id": str(ev.id), "status": ev.status}, status=202)


@login_required
def api_evaluation_status(request, eval_id):
    ev = get_object_or_404(Evaluation, id=eval_id)
    return JsonResponse(_eval_json(ev))


def _save_strategy(request, strategy):
    """Build a Strategy from POST form data and save it."""
    p = request.POST
    name = p.get("name", "").strip()
    tickers = [t.strip().upper() for t in p.get("tickers", "").split(",") if t.strip()]
    kind = p.get("kind", StrategyKind.RULE)
    schedule_kind = p.get("schedule_kind", "manual")
    enabled = p.get("enabled", "1") == "1"

    # Build kind-specific config
    if kind == StrategyKind.RULE:
        config = {"field": p.get("rule_field", "price"), "op": p.get("rule_op", ">"),
                  "value": _float(p.get("rule_value")), "direction": p.get("rule_direction", "buy")}
        code = ""
    elif kind == StrategyKind.INDICATOR:
        ind = p.get("ind_indicator", "rsi")
        config = {"indicator": ind, "op": p.get("ind_op", "<"),
                  "value": _float(p.get("ind_value", 30)), "direction": p.get("ind_direction", "buy")}
        if ind == "rsi":
            config["period"] = int(p.get("ind_period", 14) or 14)
        elif ind == "ma_cross":
            config.update({"fast": int(p.get("ind_fast", 20) or 20), "slow": int(p.get("ind_slow", 50) or 50)})
        elif ind == "macd":
            config.update({"fast": int(p.get("ind_macd_fast", 12) or 12),
                           "slow": int(p.get("ind_macd_slow", 26) or 26),
                           "signal": int(p.get("ind_macd_signal", 9) or 9)})
        code = ""
    else:  # custom_python
        config = {}
        code = p.get("code", "")

    if strategy is None:
        strategy = Strategy()
    strategy.name = name
    strategy.tickers = tickers
    strategy.kind = kind
    strategy.config = config
    strategy.code = code
    strategy.schedule_kind = schedule_kind
    strategy.interval_minutes = int(p.get("interval_minutes") or 15) if schedule_kind == "interval" else None
    strategy.daily_at = p.get("daily_at", "").strip() if schedule_kind == "daily" else ""
    strategy.enabled = enabled
    strategy.save()
    return redirect("strategy_detail", strategy_id=strategy.id)


def _float(v):
    try: return float(v)
    except (TypeError, ValueError): return None


# ── Helpers ───────────────────────────────────────────────────────────────────
def _wants_json(request):
    return "application/json" in request.headers.get("Accept", "")


def _eval_json(ev):
    return {
        "eval_id": str(ev.id),
        "strategy": ev.strategy.name if ev.strategy else None,
        "status": ev.status,
        "fired": ev.fired,
        "computed": ev.computed,
        "signals": [
            {"ticker": s.ticker, "direction": s.direction, "reason": s.reason}
            for s in ev.signals.all()
        ],
        "log": ev.log,
        "queued_at": ev.queued_at.isoformat() if ev.queued_at else None,
        "finished_at": ev.finished_at.isoformat() if ev.finished_at else None,
        "duration_ms": ev.duration_ms,
    }
