from django.dispatch import Signal

# Emitted on commit once a failed dispatch has been recorded on the scheduled job.
# Arguments: schedule_pk (int).
schedule_dispatch_failed = Signal()
