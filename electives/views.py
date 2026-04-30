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
from django.utils import timezone
from django.views.decorators.http import require_POST

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .forms import CSVUploadForm, ElectivePreferenceForm, StudentRegistrationForm
from .models import Allotment, Elective, ElectivePreference, Result, StudentProfile
from .utils import promote_best_waitlisted, reassign_student_to_best_elective


def _staff_required(view_fn):
    from functools import wraps

    @wraps(view_fn)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_staff:
            messages.error(request, "You must be a staff member to access that page.")
            return redirect("home")
        return view_fn(request, *args, **kwargs)

    return wrapper


def home(request):
    electives = Elective.objects.filter(is_active=True).order_by("code")
    return render(request, "electives/home.html", {"electives": electives})


def register_view(request):
    if request.user.is_authenticated:
        if request.user.is_staff:
            return redirect("admin_panel")
        return redirect("dashboard")
    if request.method == "POST":
        form = StudentRegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "Registration successful! Welcome.")
            return redirect("dashboard")
        messages.error(request, "Please correct the errors below.")
    else:
        form = StudentRegistrationForm()
    return render(request, "electives/register.html", {"form": form})


def login_view(request):
    if request.user.is_authenticated:
        if request.user.is_staff:
            return redirect("admin_panel")
        return redirect("dashboard")
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            if user.is_staff:
                return redirect("admin_panel")
            return redirect(request.GET.get("next", "dashboard"))
        messages.error(request, "Invalid username or password.")
    return render(request, "electives/login.html")


def logout_view(request):
    logout(request)
    return redirect("login")


# ── STUDENT DASHBOARD ─────────────────────────────────────────────────────────

@login_required
def dashboard(request):
    if request.user.is_staff:
        return redirect("admin_panel")

    try:
        profile = request.user.studentprofile
    except StudentProfile.DoesNotExist:
        messages.error(request, "No student profile found.")
        return redirect("home")

    preferences = (
        ElectivePreference.objects.filter(student=profile)
        .select_related("elective")
        .order_by("rank")
    )

    # Get all allotments for this student (normally just 1, but check for pending_choice)
    all_allotments = Allotment.objects.filter(student=profile).select_related("elective")
    allotment = all_allotments.first()

    # Pending choice: student was promoted and now has 2 confirmed seats
    pending_choice_allotments = all_allotments.filter(status="CONFIRMED", pending_choice=True)
    has_pending_choice = pending_choice_allotments.count() > 1

    # Confirmation popup — only show if not in a pending_choice situation
    show_popup = (
        not has_pending_choice
        and allotment is not None
        and allotment.status == "CONFIRMED"
        and not allotment.popup_shown
    )
    if show_popup:
        Allotment.objects.filter(student=profile, status="CONFIRMED").update(popup_shown=True)

    # Chart.js data
    flow_labels = []
    flow_values = []
    flow_colors = []

    for pref in preferences:
        flow_labels.append(f"Rank {pref.rank}: {pref.elective.code}")
        if allotment and allotment.elective_id == pref.elective_id:
            if allotment.status == "CONFIRMED":
                flow_values.append(100)
                flow_colors.append("#28a745")
            elif allotment.status == "WAITLISTED":
                flow_values.append(60)
                flow_colors.append("#ffc107")
            else:
                flow_values.append(20)
                flow_colors.append("#dc3545")
        else:
            flow_values.append(35)
            flow_colors.append("#6c757d")

    return render(request, "electives/dashboard.html", {
        "profile": profile,
        "preferences": preferences,
        "allotment": allotment,
        "show_popup": show_popup,
        "has_pending_choice": has_pending_choice,
        "pending_choice_allotments": pending_choice_allotments,
        "flow_labels_json": json.dumps(flow_labels),
        "flow_values_json": json.dumps(flow_values),
        "flow_colors_json": json.dumps(flow_colors),
    })


# ── CHOOSE ELECTIVE (when promoted with 2 confirmed seats) ───────────────────

@login_required
@require_POST
def choose_elective(request):
    if request.user.is_staff:
        return redirect("admin_panel")
    try:
        profile = request.user.studentprofile
    except StudentProfile.DoesNotExist:
        return redirect("home")

    keep_id = request.POST.get("keep_allotment_id")
    if not keep_id:
        messages.error(request, "No selection made.")
        return redirect("dashboard")

    keep_allotment = get_object_or_404(Allotment, pk=keep_id, student=profile)

    # Keep chosen one, delete the other(s)
    other_allotments = Allotment.objects.filter(
        student=profile, status="CONFIRMED"
    ).exclude(pk=keep_id)

    for other in other_allotments:
        # Free up that seat and promote next waitlisted — use signal by deleting
        other.delete()

    # Mark kept allotment as chosen
    Allotment.objects.filter(pk=keep_id).update(
        pending_choice=False,
        popup_shown=False,
    )

    messages.success(
        request,
        f"Your seat in {keep_allotment.elective.code} - {keep_allotment.elective.name} has been confirmed!"
    )
    return redirect("dashboard")


# ── OPT-IN ────────────────────────────────────────────────────────────────────

@login_required
def opt_in(request):
    if request.user.is_staff:
        return redirect("admin_panel")
    try:
        profile = request.user.studentprofile
    except StudentProfile.DoesNotExist:
        messages.error(request, "No student profile found.")
        return redirect("home")

    if Allotment.objects.filter(student=profile, status="CONFIRMED").exists():
        messages.warning(request, "You already have a confirmed allotment. Withdraw it first to change preferences.")
        return redirect("dashboard")

    existing_prefs = ElectivePreference.objects.filter(student=profile).order_by("rank")

    if request.method == "POST":
        form = ElectivePreferenceForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                ElectivePreference.objects.filter(student=profile).delete()
                rank_map = {
                    1: form.cleaned_data["first_choice"],
                    2: form.cleaned_data["second_choice"],
                    3: form.cleaned_data["third_choice"],
                }
                for rank, elective in rank_map.items():
                    ElectivePreference.objects.create(student=profile, elective=elective, rank=rank)
            messages.success(request, "Your preferences have been saved successfully!")
            return redirect("dashboard")
        messages.error(request, "Please fix the errors below.")
    else:
        initial = {}
        label_map = {1: "first_choice", 2: "second_choice", 3: "third_choice"}
        for pref in existing_prefs:
            initial[label_map[pref.rank]] = pref.elective
        form = ElectivePreferenceForm(initial=initial)

    electives = Elective.objects.filter(is_active=True).order_by("code")
    return render(request, "electives/opt_in.html", {
        "form": form,
        "electives": electives,
        "existing_prefs": existing_prefs,
    })


# ── RESULTS ───────────────────────────────────────────────────────────────────

@login_required
def results(request):
    if request.user.is_staff:
        branch_filter = request.GET.get("branch", "")
        qs = Allotment.objects.select_related("student__user", "elective").order_by(
            "student__branch", "student__usn"
        )
        if branch_filter:
            qs = qs.filter(student__branch__iexact=branch_filter)
        branches = StudentProfile.objects.values_list("branch", flat=True).distinct().order_by("branch")
        return render(request, "electives/results.html", {
            "all_allotments": qs,
            "allotments": qs,
            "branches": branches,
            "branch_filter": branch_filter,
            "is_staff": True,
        })

    try:
        profile = request.user.studentprofile
    except StudentProfile.DoesNotExist:
        messages.error(request, "No student profile found.")
        return redirect("home")

    allotment = Allotment.objects.filter(student=profile).select_related("elective").first()
    prefs = ElectivePreference.objects.filter(student=profile).select_related("elective").order_by("rank")

    flow = []
    for pref in prefs:
        if allotment and allotment.elective_id == pref.elective_id:
            status = allotment.status
            wpos = allotment.waitlist_position
        else:
            status = "PENDING"
            wpos = None
        flow.append({
            "rank": pref.rank,
            "elective": pref.elective,
            "status": status,
            "waitlist_position": wpos,
        })

    return render(request, "electives/results.html", {
        "allotment": allotment,
        "preferences": prefs,
        "flow": flow,
        "is_staff": False,
    })


# ── WITHDRAW ──────────────────────────────────────────────────────────────────

@login_required
@require_POST
def withdraw_allotment(request):
    if request.user.is_staff:
        return redirect("admin_panel")
    try:
        profile = request.user.studentprofile
    except StudentProfile.DoesNotExist:
        messages.error(request, "No student profile found.")
        return redirect("home")

    allotment = Allotment.objects.filter(student=profile, status="CONFIRMED").first()
    if allotment:
        allotment.delete()  # triggers post_delete signal -> auto-promotion
        messages.success(request, "Your allotment has been withdrawn. The next eligible student has been promoted.")
    else:
        messages.warning(request, "No confirmed allotment found to withdraw.")
    return redirect("dashboard")


# ── SEAT STATUS API ───────────────────────────────────────────────────────────

def seat_status(request):
    electives_payload = []
    for e in Elective.objects.filter(is_active=True).order_by("code"):
        confirmed = Allotment.objects.filter(elective=e, status="CONFIRMED").count()
        waitlisted = Allotment.objects.filter(elective=e, status="WAITLISTED").count()
        available = max(0, e.total_seats - confirmed)
        pct = int((available / e.total_seats) * 100) if e.total_seats else 0

        bq_payload = {}
        for b, q in (e.branch_quota or {}).items():
            used_b = Allotment.objects.filter(elective=e, status="CONFIRMED", student__branch=b).count()
            bq_payload[b] = {"quota": q, "used": used_b, "available": max(0, q - used_b)}

        electives_payload.append({
            "id": e.id,
            "code": e.code,
            "name": e.name,
            "total": e.total_seats,
            "confirmed": confirmed,
            "waitlisted": waitlisted,
            "available": available,
            "percentage": pct,
            "branch_quotas": bq_payload,
        })

    return JsonResponse({"electives": electives_payload})


# ── AJAX POPUP ENDPOINTS ──────────────────────────────────────────────────────

@login_required
def mark_popup_seen(request):
    if request.method == "POST":
        try:
            profile = request.user.studentprofile
            Allotment.objects.filter(
                student=profile, status="CONFIRMED", popup_shown=False
            ).update(popup_shown=True)
            return JsonResponse({"status": "ok"})
        except Exception:
            return JsonResponse({"status": "error"}, status=400)
    return JsonResponse({"status": "method_not_allowed"}, status=405)


@login_required
def check_new_confirmations(request):
    try:
        profile = request.user.studentprofile
        allotment = Allotment.objects.filter(
            student=profile, status="CONFIRMED", popup_shown=False
        ).select_related("elective").first()
        if allotment:
            return JsonResponse({
                "has_new": True,
                "elective_name": allotment.elective.name if allotment.elective else "",
                "elective_code": allotment.elective.code if allotment.elective else "",
                "rank": allotment.preference_rank_given,
            })
        return JsonResponse({"has_new": False})
    except Exception:
        return JsonResponse({"has_new": False})


# ── ANALYTICS ─────────────────────────────────────────────────────────────────

def analytics_data(request):
    electives = list(Elective.objects.filter(is_active=True).values("id", "name", "total_seats"))
    pref_dist = []
    for e in electives:
        pref_dist.append({
            "elective": e["name"],
            "rank1": ElectivePreference.objects.filter(elective_id=e["id"], rank=1).count(),
            "rank2": ElectivePreference.objects.filter(elective_id=e["id"], rank=2).count(),
            "rank3": ElectivePreference.objects.filter(elective_id=e["id"], rank=3).count(),
        })

    branch_demand_qs = (
        ElectivePreference.objects.values("student__branch")
        .annotate(count=Count("id"))
        .order_by("-count")
    )
    branch_demand = [{"branch": r["student__branch"], "count": r["count"]} for r in branch_demand_qs]
    status_counts = dict(
        Allotment.objects.values("status").annotate(c=Count("id")).values_list("status", "c")
    )

    seat_fill = []
    for e in Elective.objects.filter(is_active=True).order_by("code"):
        filled = Allotment.objects.filter(elective=e, status="CONFIRMED").count()
        seat_fill.append({
            "elective": e.name,
            "total": e.total_seats,
            "filled": filled,
            "pct": round((filled / e.total_seats * 100) if e.total_seats else 0, 1),
        })

    bands = [("9-10", 9.0, 10.1), ("8-9", 8.0, 9.0), ("7-8", 7.0, 8.0), ("6-7", 6.0, 7.0), ("<6", 0.0, 6.0)]
    cgpa_bands = []
    for label, lo, hi in bands:
        qs = Allotment.objects.filter(student__cgpa__gte=lo, student__cgpa__lt=hi)
        cgpa_bands.append({
            "band": label,
            "confirmed": qs.filter(status="CONFIRMED").count(),
            "not_allotted": qs.filter(status="NOT_ALLOTTED").count(),
        })

    total = Allotment.objects.count()
    confirmed = status_counts.get("CONFIRMED", 0)
    not_allotted = status_counts.get("NOT_ALLOTTED", 0)

    return JsonResponse({
        "preference_dist": pref_dist,
        "branch_demand": branch_demand,
        "allotment_status": status_counts,
        "seat_fill": seat_fill,
        "cgpa_bands": cgpa_bands,
        "allotment_summary": {"total": total, "confirmed": confirmed, "not_allotted": not_allotted},
    })


@login_required
def analytics_dashboard(request):
    return render(request, "electives/analytics.html")


# ── ADMIN PANEL ───────────────────────────────────────────────────────────────

@_staff_required
def admin_panel(request):
    total_students = StudentProfile.objects.count()
    total_prefs = ElectivePreference.objects.count()
    confirmed = Allotment.objects.filter(status="CONFIRMED").count()
    not_allotted = Allotment.objects.filter(status="NOT_ALLOTTED").count()
    waitlisted = Allotment.objects.filter(status="WAITLISTED").count()

    electives = Elective.objects.filter(is_active=True).order_by("code")
    elective_stats = []
    for e in electives:
        filled = Allotment.objects.filter(elective=e, status="CONFIRMED").count()
        wl = Allotment.objects.filter(elective=e, status="WAITLISTED").count()
        demand = ElectivePreference.objects.filter(elective=e, rank=1).count()

        quota_vs_actual = []
        for branch, quota in (e.branch_quota or {}).items():
            actual = Allotment.objects.filter(
                elective=e, status="CONFIRMED", student__branch=branch
            ).count()
            quota_vs_actual.append({
                "branch": branch,
                "quota": quota,
                "actual": actual,
                "remaining": max(0, quota - actual),
            })

        elective_stats.append({
            "elective": e,
            "filled": filled,
            "waitlisted": wl,
            "demand": demand,
            "remaining": max(0, e.total_seats - filled),
            "quota_vs_actual": quota_vs_actual,
        })

    all_allotments = Allotment.objects.select_related(
        "student__user", "elective"
    ).order_by("student__branch", "student__usn")

    confirmed_allotments = all_allotments.filter(status="CONFIRMED")
    waitlisted_allotments = all_allotments.filter(status="WAITLISTED").order_by(
        "elective__code", "waitlist_position"
    )

    all_students = StudentProfile.objects.select_related("user").order_by("usn")

    recent_allotments = Allotment.objects.select_related(
        "student__user", "elective"
    ).order_by("-allotted_at")[:15]

    return render(request, "electives/admin_panel.html", {
        "total_students": total_students,
        "total_prefs": total_prefs,
        "confirmed": confirmed,
        "not_allotted": not_allotted,
        "waitlisted": waitlisted,
        "elective_stats": elective_stats,
        "all_allotments": all_allotments,
        "confirmed_allotments": confirmed_allotments,
        "waitlisted_allotments": waitlisted_allotments,
        "all_students": all_students,
        "recent_allotments": recent_allotments,
    })


# ── RUN ALLOTMENT ─────────────────────────────────────────────────────────────

@_staff_required
def run_allotment(request):
    if request.method == "POST":
        with transaction.atomic():
            Allotment.objects.filter(status="NOT_ALLOTTED").delete()
            Allotment.objects.filter(status="WAITLISTED").delete()

            active_electives = list(Elective.objects.select_for_update().filter(is_active=True))
            filled = {e.id: Allotment.objects.filter(elective=e, status="CONFIRMED").count() for e in active_electives}
            branch_filled = {e.id: {} for e in active_electives}
            for e in active_electives:
                for b in (e.branch_quota or {}):
                    branch_filled[e.id][b] = Allotment.objects.filter(
                        elective=e, status="CONFIRMED", student__branch=b
                    ).count()

            all_profiles = list(StudentProfile.objects.select_related("user").all())
            already_confirmed = set(
                Allotment.objects.filter(status="CONFIRMED").values_list("student_id", flat=True)
            )

            prefs_by_student = {}
            for pref in ElectivePreference.objects.select_related("elective").order_by("submitted_at"):
                prefs_by_student.setdefault(pref.student_id, {})[pref.rank] = pref

            sorted_profiles = sorted(
                all_profiles,
                key=lambda p: (
                    prefs_by_student.get(p.id, {}).get(1, None) and
                    prefs_by_student[p.id][1].submitted_at or timezone.now()
                )
            )

            new_allotments = []
            waitlist_positions = {e.id: 1 for e in active_electives}

            for profile in sorted_profiles:
                if profile.id in already_confirmed:
                    continue

                student_prefs = prefs_by_student.get(profile.id, {})
                allotted = False

                for rank in [1, 2, 3]:
                    pref_obj = student_prefs.get(rank)
                    if not pref_obj:
                        continue
                    elective = pref_obj.elective

                    if filled[elective.id] >= elective.total_seats:
                        continue

                    bq = elective.branch_quota or {}
                    branch = profile.branch
                    if bq and branch in bq:
                        if branch_filled[elective.id].get(branch, 0) >= int(bq[branch]):
                            continue

                    filled[elective.id] += 1
                    branch_filled[elective.id][branch] = branch_filled[elective.id].get(branch, 0) + 1

                    new_allotments.append(Allotment(
                        student=profile,
                        elective=elective,
                        status="CONFIRMED",
                        preference_rank_given=rank,
                        popup_shown=False,
                    ))
                    allotted = True
                    break

                if not allotted and student_prefs:
                    first_pref = student_prefs.get(1) or list(student_prefs.values())[0]
                    elective = first_pref.elective
                    wpos = waitlist_positions[elective.id]
                    waitlist_positions[elective.id] += 1
                    new_allotments.append(Allotment(
                        student=profile,
                        elective=elective,
                        status="WAITLISTED",
                        preference_rank_given=1,
                        waitlist_position=wpos,
                    ))

            Allotment.objects.bulk_create(new_allotments)

        conf = sum(1 for a in new_allotments if a.status == "CONFIRMED")
        wl = sum(1 for a in new_allotments if a.status == "WAITLISTED")
        messages.success(request, f"Allotment complete! {conf} confirmed, {wl} waitlisted.")
        return redirect("admin_panel")

    context = {
        "total_students": ElectivePreference.objects.values("student").distinct().count(),
        "total_electives": Elective.objects.filter(is_active=True).count(),
        "already_allotted": Allotment.objects.filter(status="CONFIRMED").count(),
    }
    return render(request, "electives/run_allotment.html", context)


# ── ADMIN OVERRIDE: FORCE CONFIRM ─────────────────────────────────────────────

@_staff_required
def admin_force_confirm(request, allotment_id):
    allotment = get_object_or_404(Allotment, pk=allotment_id)
    Allotment.objects.filter(pk=allotment_id).update(
        status="CONFIRMED",
        is_admin_override=True,
        popup_shown=False,
        pending_choice=False,
        admin_note=f"Force confirmed by admin on {timezone.now().strftime('%Y-%m-%d %H:%M')}",
    )
    messages.success(request, f"{allotment.student.usn} force confirmed for {allotment.elective.code}.")
    return redirect("admin_panel")


# ── ADMIN OVERRIDE: RESET TO WAITLIST ─────────────────────────────────────────

@_staff_required
def admin_reset_waitlist(request, allotment_id):
    allotment = get_object_or_404(Allotment, pk=allotment_id)
    was_confirmed = allotment.status == "CONFIRMED"
    elective = allotment.elective

    Allotment.objects.filter(pk=allotment_id).update(
        status="WAITLISTED",
        is_admin_override=True,
        pending_choice=False,
        admin_note=f"Reset to waitlist by admin on {timezone.now().strftime('%Y-%m-%d %H:%M')}",
    )

    if was_confirmed and elective:
        promote_best_waitlisted(elective)

    messages.success(request, f"{allotment.student.usn} reset to waitlisted.")
    return redirect("admin_panel")


# ── ADMIN OVERRIDE: REASSIGN ──────────────────────────────────────────────────

@_staff_required
def admin_reassign(request, student_id):
    student = get_object_or_404(StudentProfile, pk=student_id)
    result = reassign_student_to_best_elective(student)
    if result:
        messages.success(request, f"{student.usn} reassigned to {result.elective.code} (Rank {result.preference_rank_given}).")
    else:
        messages.warning(request, f"Could not reassign {student.usn}: no eligible seats available across all preferences.")
    return redirect("admin_panel")


# ── EXPORT ────────────────────────────────────────────────────────────────────

@_staff_required
def export_allotment_csv(request):
    branch_filter = request.GET.get("branch", "").strip()
    qs = Allotment.objects.select_related("student__user", "elective").order_by("student__branch", "student__usn")
    if branch_filter:
        qs = qs.filter(student__branch__iexact=branch_filter)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="allotment_{branch_filter or "all"}.csv"'
    writer = csv.writer(response)
    writer.writerow(["USN", "Student Name", "Branch", "CGPA", "Elective", "Code", "Status", "Rank", "Waitlist Pos", "Admin Override"])
    for a in qs:
        writer.writerow([
            a.student.usn,
            a.student.user.get_full_name() or a.student.user.username,
            a.student.branch,
            a.student.cgpa,
            a.elective.name if a.elective else "N/A",
            a.elective.code if a.elective else "N/A",
            a.status,
            a.preference_rank_given or "N/A",
            a.waitlist_position or "",
            "Yes" if a.is_admin_override else "No",
        ])
    return response


@_staff_required
def export_allotment_pdf(request):
    branch = request.GET.get("branch")
    allotments = Allotment.objects.select_related("student__user", "elective").filter(status="CONFIRMED")
    if branch:
        allotments = allotments.filter(student__branch__iexact=branch)

    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="allotment_{branch or "all"}.pdf"'
    doc = SimpleDocTemplate(response, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = [Paragraph("Elective Allotment Report", styles["Title"])]
    if branch:
        elements.append(Paragraph(f"Branch: {branch}", styles["Normal"]))
    elements.append(Spacer(1, 12))

    data = [["USN", "Student Name", "Branch", "CGPA", "Elective", "Rank"]]
    for a in allotments:
        data.append([
            a.student.usn,
            a.student.user.get_full_name() or a.student.user.username,
            a.student.branch,
            str(a.student.cgpa),
            a.elective.name if a.elective else "N/A",
            str(a.preference_rank_given or "-"),
        ])

    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#343a40")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8f9fa")]),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    elements.append(table)
    doc.build(elements)
    return response


# ── UPLOAD RESULTS ────────────────────────────────────────────────────────────

@login_required
def upload_results(request):
    errors = []
    success_count = 0
    if request.method == "POST":
        form = CSVUploadForm(request.POST, request.FILES)
        if form.is_valid():
            f = form.cleaned_data["csv_file"]
            decoded = f.read().decode("utf-8", errors="ignore")
            reader = csv.DictReader(io.StringIO(decoded))
            required_cols = {"usn", "student_name", "branch", "subject", "marks"}
            if not reader.fieldnames:
                errors.append("CSV header row is missing.")
            else:
                normalized = {h.strip().lower() for h in reader.fieldnames}
                missing = required_cols - normalized
                if missing:
                    errors.append(f"Missing required columns: {', '.join(sorted(missing))}")
            if not errors:
                for i, row in enumerate(reader, start=2):
                    try:
                        usn = str(row.get("usn", "")).strip()
                        student_name = str(row.get("student_name", "")).strip()
                        branch = str(row.get("branch", "")).strip()
                        subject = str(row.get("subject", "")).strip()
                        marks = float(row.get("marks", ""))
                        if not usn or not student_name or not branch or not subject:
                            raise ValueError("Empty required field(s)")
                        if marks < 0 or marks > 100:
                            raise ValueError("marks must be between 0 and 100")
                        grade = "O" if marks >= 90 else "A+" if marks >= 80 else "A" if marks >= 70 else "B+" if marks >= 60 else "B" if marks >= 50 else "C" if marks >= 40 else "F"
                        Result.objects.create(usn=usn, student_name=student_name, branch=branch, subject=subject, marks=marks, grade=grade)
                        success_count += 1
                    except Exception as e:
                        errors.append(f"Row {i}: {e}")
    else:
        form = CSVUploadForm()

    return render(request, "electives/upload.html", {
        "form": form,
        "errors": errors,
        "success_count": success_count,
        "total_records": Result.objects.count(),
        "total_branches": Result.objects.values("branch").distinct().count(),
        "total_subjects": Result.objects.values("subject").distinct().count(),
    })


def download_sample_csv(request):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="sample_results.csv"'
    writer = csv.writer(response)
    writer.writerow(["usn", "student_name", "branch", "subject", "marks"])
    writer.writerow(["1RV21CS001", "Asha Reddy", "CSE", "Machine Learning", 87])
    writer.writerow(["1RV21IS042", "Rahul Sharma", "ISE", "Cloud Computing", 74])
    writer.writerow(["1RV21EC019", "Priya Nair", "ECE", "IoT Systems", 91])
    return response
# ── ADMIN LIVE DATA API ───────────────────────────────────────────────────────

@_staff_required
def admin_live_data(request):
    """Returns live allotment table data as JSON for admin dashboard AJAX refresh."""
    allotments = Allotment.objects.select_related(
        "student__user", "elective"
    ).order_by("student__branch", "student__usn")

    rows = []
    for a in allotments:
        rows.append({
            "usn": a.student.usn,
            "name": a.student.user.get_full_name() or a.student.user.username,
            "branch": a.student.branch,
            "cgpa": str(a.student.cgpa),
            "elective_code": a.elective.code if a.elective else "",
            "elective_name": a.elective.name if a.elective else "",
            "status": a.status,
            "rank": a.preference_rank_given or "",
            "waitlist_position": a.waitlist_position or "",
            "is_admin_override": a.is_admin_override,
            "allotment_id": a.id,
            "student_id": a.student.id,
            "has_prefs": ElectivePreference.objects.filter(student=a.student).exists(),
        })

    confirmed = Allotment.objects.filter(status="CONFIRMED").count()
    waitlisted = Allotment.objects.filter(status="WAITLISTED").count()
    not_allotted = Allotment.objects.filter(status="NOT_ALLOTTED").count()

    return JsonResponse({
        "allotments": rows,
        "stats": {
            "confirmed": confirmed,
            "waitlisted": waitlisted,
            "not_allotted": not_allotted,
        }
    })
path = "/Users/lekhanap/django-semihack-2026-ctrl-v/elective_portal/electives/urls.py"
with open(path, "r") as f:
    content = f.read()

old = '    path("admin/reassign/<int:student_id>/", views.admin_reassign, name="admin_reassign"),'
new = '    path("admin/reassign/<int:student_id>/", views.admin_reassign, name="admin_reassign"),\n    path("api/admin-live/", views.admin_live_data, name="admin_live_data"),'

if old in content:
    content = content.replace(old, new)
    print("OK")
else:
    print("NOT FOUND - add manually")

with open(path, "w") as f:
    f.write(content)