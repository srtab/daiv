from django.db import models


class ChatThreadManager(models.Manager):
    def for_user(self, user):
        return self.filter(user=user)
