from django.contrib.auth.models import User
from django.db import models


class StudentProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    usn = models.CharField(max_length=20, unique=True)
    cgpa = models.DecimalField(max_digits=4, decimal_places=2)
    semester = models.IntegerField(default=6)
    branch = models.CharField(max_length=50, default="CSE")

    def __str__(self):
        return f"{self.user.get_full_name()} ({self.usn}) - CGPA: {self.cgpa}"


class Elective(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20, unique=True)
    faculty = models.CharField(max_length=100)
    total_seats = models.IntegerField(default=60)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    branch_quota = models.JSONField(
        default=dict,
        blank=True,
        help_text='Branch-wise seat quota. Example: {"CSE": 40, "ISE": 15, "ECE": 5}.',
    )

    def available_seats(self, branch=None):
        if branch and self.branch_quota and branch in self.branch_quota:
            quota = self.branch_quota[branch]
            used = self.allotment_set.filter(status="CONFIRMED", student__branch=branch).count()
            return max(0, quota - used)
        allotted = self.allotment_set.filter(status="CONFIRMED").count()
        return max(0, self.total_seats - allotted)

    def confirmed_count(self):
        return self.allotment_set.filter(status="CONFIRMED").count()

    def waitlisted_count(self):
        return self.allotment_set.filter(status="WAITLISTED").count()

    def get_quota_display(self):
        if self.branch_quota:
            return ", ".join(f"{b}: {s}" for b, s in self.branch_quota.items())
        return f"General: {self.total_seats}"

    def __str__(self):
        return f"{self.code} - {self.name} ({self.available_seats()} seats left)"


class ElectivePreference(models.Model):
    RANK_CHOICES = [(1, "1st Choice"), (2, "2nd Choice"), (3, "3rd Choice")]
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE)
    elective = models.ForeignKey(Elective, on_delete=models.CASCADE)
    rank = models.IntegerField(choices=RANK_CHOICES)
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("student", "elective"), ("student", "rank")]
        ordering = ["submitted_at"]

    def __str__(self):
        return f"{self.student.usn} -> Rank {self.rank}: {self.elective.code}"


class Allotment(models.Model):
    STATUS_CHOICES = [
        ("CONFIRMED", "Confirmed"),
        ("WAITLISTED", "Waitlisted"),
        ("NOT_ALLOTTED", "Not Allotted"),
    ]
    student = models.ForeignKey(StudentProfile, on_delete=models.CASCADE)
    elective = models.ForeignKey(Elective, on_delete=models.CASCADE, null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="NOT_ALLOTTED")
    allotted_at = models.DateTimeField(auto_now_add=True)
    preference_rank_given = models.IntegerField(null=True, blank=True)
    waitlist_position = models.IntegerField(null=True, blank=True)
    popup_shown = models.BooleanField(default=False)
    is_admin_override = models.BooleanField(default=False)
    admin_note = models.TextField(blank=True)
    pending_choice = models.BooleanField(default=False)

    class Meta:
        unique_together = [("student",)]

    def __str__(self):
        elective_label = self.elective.code if self.elective else "N/A"
        return f"{self.student.usn} -> {elective_label} [{self.status}]"


class Result(models.Model):
    usn = models.CharField(max_length=20)
    student_name = models.CharField(max_length=100)
    branch = models.CharField(max_length=50)
    subject = models.CharField(max_length=100)
    marks = models.FloatField()
    grade = models.CharField(max_length=5, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.usn} - {self.subject} - {self.grade or 'NA'}"
