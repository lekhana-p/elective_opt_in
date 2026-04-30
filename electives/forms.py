from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User

from .models import StudentProfile, Elective


class StudentRegistrationForm(UserCreationForm):
    first_name = forms.CharField(max_length=50)
    last_name = forms.CharField(max_length=50)
    usn = forms.CharField(max_length=20, label="USN")
    cgpa = forms.DecimalField(max_digits=4, decimal_places=2, min_value=0, max_value=10)
    semester = forms.IntegerField(min_value=1, max_value=8)
    branch = forms.CharField(max_length=50, initial="CSE")

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email", "password1", "password2"]

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        user.email = self.cleaned_data.get("email", "")
        if commit:
            user.save()
            StudentProfile.objects.create(
                user=user,
                usn=self.cleaned_data["usn"],
                cgpa=self.cleaned_data["cgpa"],
                semester=self.cleaned_data["semester"],
                branch=self.cleaned_data["branch"],
            )
        return user


class ElectivePreferenceForm(forms.Form):
    first_choice = forms.ModelChoiceField(
        queryset=Elective.objects.none(),
        label="1st Choice (Highest Priority)",
        empty_label="-- Select Elective --",
    )
    second_choice = forms.ModelChoiceField(
        queryset=Elective.objects.none(),
        label="2nd Choice",
        empty_label="-- Select Elective --",
    )
    third_choice = forms.ModelChoiceField(
        queryset=Elective.objects.none(),
        label="3rd Choice",
        empty_label="-- Select Elective --",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Elective.objects.filter(is_active=True).order_by("code")
        self.fields["first_choice"].queryset = qs
        self.fields["second_choice"].queryset = qs
        self.fields["third_choice"].queryset = qs

    def clean(self):
        cleaned = super().clean()
        c1 = cleaned.get("first_choice")
        c2 = cleaned.get("second_choice")
        c3 = cleaned.get("third_choice")

        choices = [c for c in [c1, c2, c3] if c]
        if len(choices) != len({c.id for c in choices}):
            raise forms.ValidationError("All 3 choices must be different electives!")
        return cleaned


class CSVUploadForm(forms.Form):
    csv_file = forms.FileField(
        label="Upload CSV File",
        help_text="Only .csv files are accepted.",
        widget=forms.FileInput(attrs={"accept": ".csv"}),
    )

    def clean_csv_file(self):
        f = self.cleaned_data["csv_file"]
        if not f.name.lower().endswith(".csv"):
            raise forms.ValidationError("Only .csv files are allowed.")
        return f