from django.db import IntegrityError

import pytest

from codebase.models import CrossProjectAccessRecord


@pytest.mark.django_db
def test_a_record_without_an_outcome_is_rejected():
    with pytest.raises(IntegrityError):
        CrossProjectAccessRecord.objects.create(provider="gitlab", target_repo_id="g/other", outcome="")
