from django.utils import timezone
from .models import Allotment, ElectivePreference


def promote_best_waitlisted(elective):
    """
    Promote best WAITLISTED student when a CONFIRMED seat is freed.
    Priority: preference_rank -> CGPA -> submitted_at -> USN
    """
    confirmed_count = Allotment.objects.filter(elective=elective, status="CONFIRMED").count()
    if confirmed_count >= elective.total_seats:
        return None  # Still full

    candidates = (
        Allotment.objects
        .filter(elective=elective, status="WAITLISTED")
        .select_related("student")
        .order_by("preference_rank_given", "-student__cgpa", "allotted_at", "student__usn")
    )

    for candidate in candidates:
        student = candidate.student
        branch = student.branch
        bq = elective.branch_quota or {}

        if bq and branch in bq:
            used = Allotment.objects.filter(
                elective=elective, status="CONFIRMED", student__branch=branch
            ).count()
            if used >= int(bq[branch]):
                continue  # Branch quota full, try next

        Allotment.objects.filter(pk=candidate.pk).update(
            status="CONFIRMED",
            waitlist_position=None,
            popup_shown=False,
            pending_choice=False,
        )
        _reorder_waitlist(elective)
        return Allotment.objects.get(pk=candidate.pk)

    return None


def _reorder_waitlist(elective):
    waitlisted = Allotment.objects.filter(
        elective=elective, status="WAITLISTED"
    ).order_by("allotted_at")
    for i, a in enumerate(waitlisted, start=1):
        if a.waitlist_position != i:
            Allotment.objects.filter(pk=a.pk).update(waitlist_position=i)


def reassign_student_to_best_elective(student):
    """
    Admin override: reassign student to first available eligible preferred elective.
    Requires student to have submitted preferences.
    """
    prefs = (
        ElectivePreference.objects
        .filter(student=student)
        .select_related("elective")
        .order_by("rank")
    )

    if not prefs.exists():
        return None  # No preferences submitted

    for pref in prefs:
        elective = pref.elective
        if not elective.is_active:
            continue

        # Check if student already confirmed here
        already_here = Allotment.objects.filter(
            student=student, elective=elective, status="CONFIRMED"
        ).exists()
        if already_here:
            return Allotment.objects.get(student=student)

        confirmed_total = Allotment.objects.filter(
            elective=elective, status="CONFIRMED"
        ).count()
        if confirmed_total >= elective.total_seats:
            continue

        bq = elective.branch_quota or {}
        if bq and student.branch in bq:
            used = Allotment.objects.filter(
                elective=elective, status="CONFIRMED", student__branch=student.branch
            ).count()
            if used >= int(bq[student.branch]):
                continue

        # Safe update_or_create — unique on student only
        allotment, created = Allotment.objects.update_or_create(
            student=student,
            defaults=dict(
                elective=elective,
                status="CONFIRMED",
                preference_rank_given=pref.rank,
                is_admin_override=True,
                admin_note=f"Reassigned by admin on {timezone.now().strftime('%Y-%m-%d %H:%M')}",
                popup_shown=False,
                waitlist_position=None,
                pending_choice=False,
            )
        )
        return allotment

    return None
