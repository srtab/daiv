from notifications.choices import DeliveryStatus, NotifyOn


class TestNotifyOn:
    def test_has_four_values(self):
        assert {NotifyOn.NEVER, NotifyOn.ALWAYS, NotifyOn.ON_SUCCESS, NotifyOn.ON_FAILURE} == set(NotifyOn)

    def test_default_is_never(self):
        assert NotifyOn.NEVER.value == "never"


class TestDeliveryStatus:
    def test_has_four_values(self):
        assert set(DeliveryStatus.values) == {"pending", "sent", "failed", "skipped"}
