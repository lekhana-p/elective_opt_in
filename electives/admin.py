from django.contrib import admin
from .models import StudentProfile, Elective, ElectivePreference, Allotment, Result

admin.site.register(StudentProfile)
admin.site.register(Elective)
admin.site.register(ElectivePreference)
admin.site.register(Allotment)
admin.site.register(Result)
