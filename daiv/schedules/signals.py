from django.dispatch import Signal

# Emitted on commit after every failed dispatch of a scheduled job.
# Arguments: schedule_pk (int).
schedule_dispatch_failed = Signal()
