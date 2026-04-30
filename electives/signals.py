from django.db.models.signals import post_delete
from django.dispatch import receiver
from .models import Allotment


@receiver(post_delete, sender=Allotment)
def promote_on_withdrawal(sender, instance, **kwargs):
    """
    When a CONFIRMED allotment is deleted (student withdraws or admin removes),
    automatically promote the best WAITLISTED student for that elective.
    """
    if instance.status != "CONFIRMED":
        return
    if not instance.elective_id:
        return

    from .models import Elective
    try:
        elective = Elective.objects.get(pk=instance.elective_id)
    except Elective.DoesNotExist:
        return

    from .utils import promote_best_waitlisted
    promoted = promote_best_waitlisted(elective)

    # If promoted student already has another CONFIRMED allotment elsewhere,
    # flag both as pending_choice so student can pick one
    if promoted:
        student = promoted.student
        confirmed_allotments = Allotment.objects.filter(
            student=student, status="CONFIRMED"
        )
        if confirmed_allotments.count() > 1:
            confirmed_allotments.update(pending_choice=True)
