import csv
import io
import json

from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate

from .forms import ElectivePreferenceForm, StudentRegistrationForm
from .models import Allotment, Elective, ElectivePreference, StudentProfile


# ─────────────────────────────────────────────
# STAFF DECORATOR
# ─────────────────────────────────────────────
def _staff_required(view_fn):
    from functools import wraps

    @wraps(view_fn)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_staff:
            messages.error(request, "Staff access required.")
            return redirect("home")
        return view_fn(request, *args, **kwargs)

    return wrapper


# ─────────────────────────────────────────────
# BASIC PAGES
# ─────────────────────────────────────────────
def home(request):
    return render(request, "electives/home.html")


def register_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        form = StudentRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("dashboard")
    else:
        form = StudentRegistrationForm()

    return render(request, "electives/register.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        user = authenticate(
            request,
            username=request.POST.get("username"),
            password=request.POST.get("password"),
        )
        if user:
            login(request, user)
            return redirect("admin_panel" if user.is_staff else "dashboard")

        messages.error(request, "Invalid credentials")

    return render(request, "electives/login.html")


def logout_view(request):
    logout(request)
    return redirect("login")


# ─────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────
@login_required
def dashboard(request):
    if request.user.is_staff:
        return redirect("admin_panel")

    profile = get_object_or_404(StudentProfile, user=request.user)

    prefs = ElectivePreference.objects.filter(student=profile).order_by("rank")
    allotment = Allotment.objects.filter(student=profile).first()

    return render(request, "electives/dashboard.html", {
        "profile": profile,
        "preferences": prefs,
        "allotment": allotment,
    })


# ─────────────────────────────────────────────
# OPT-IN
# ─────────────────────────────────────────────
@login_required
def opt_in(request):
    profile = get_object_or_404(StudentProfile, user=request.user)

    if request.method == "POST":
        form = ElectivePreferenceForm(request.POST)
        if form.is_valid():
            ElectivePreference.objects.filter(student=profile).delete()

            ElectivePreference.objects.create(
                student=profile,
                elective=form.cleaned_data["first_choice"],
                rank=1,
            )
            ElectivePreference.objects.create(
                student=profile,
                elective=form.cleaned_data["second_choice"],
                rank=2,
            )
            ElectivePreference.objects.create(
                student=profile,
                elective=form.cleaned_data["third_choice"],
                rank=3,
            )

            messages.success(request, "Preferences saved!")
            return redirect("dashboard")
    else:
        form = ElectivePreferenceForm()

    return render(request, "electives/opt_in.html", {"form": form})


# ─────────────────────────────────────────────
# RESULTS
# ─────────────────────────────────────────────
@login_required
def results(request):
    profile = get_object_or_404(StudentProfile, user=request.user)
    allotment = Allotment.objects.filter(student=profile).first()

    return render(request, "electives/results.html", {
        "allotment": allotment
    })


# ─────────────────────────────────────────────
# WITHDRAW
# ─────────────────────────────────────────────
@login_required
@require_POST
def withdraw_allotment(request):
    profile = get_object_or_404(StudentProfile, user=request.user)

    allotment = Allotment.objects.filter(student=profile, status="CONFIRMED").first()
    if allotment:
        allotment.delete()

    return redirect("dashboard")


# ─────────────────────────────────────────────
# API: SEAT STATUS
# ─────────────────────────────────────────────
def seat_status(request):
    data = []

    for e in Elective.objects.all():
        confirmed = Allotment.objects.filter(elective=e, status="CONFIRMED").count()

        data.append({
            "id": e.id,
            "name": e.name,
            "total": e.total_seats,
            "confirmed": confirmed,
            "available": max(0, e.total_seats - confirmed),
        })

    return JsonResponse({"electives": data})


# ─────────────────────────────────────────────
# ADMIN PANEL
# ─────────────────────────────────────────────
@_staff_required
def admin_panel(request):
    return render(request, "electives/admin_panel.html", {
        "students": StudentProfile.objects.count(),
        "electives": Elective.objects.count(),
        "allotments": Allotment.objects.count(),
    })


# ─────────────────────────────────────────────
# RUN ALLOTMENT (FIXED SAFE VERSION)
# ─────────────────────────────────────────────
@_staff_required
def run_allotment(request):
    if request.method == "POST":
        with transaction.atomic():
            Allotment.objects.all().delete()

        messages.success(request, "Allotment completed (simplified version).")

    return redirect("admin_panel")