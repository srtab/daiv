from __future__ import annotations

import io

from django import forms
from django.utils.translation import gettext_lazy as _

from skills.constants import MAX_ZIP_BYTES
from skills.services import SkillPackage, SkillValidationError


class SkillUploadForm(forms.Form):
    """Form for uploading a zipped skill. Validation delegates to
    ``SkillPackage.inspect`` so the form layer stays thin."""

    zip = forms.FileField(label=_("Skill zip"), required=True)
    force = forms.BooleanField(label=_("Replace existing"), required=False)

    def clean_zip(self):
        uploaded = self.cleaned_data["zip"]
        if uploaded.size > MAX_ZIP_BYTES:
            raise forms.ValidationError(_("Zip exceeds maximum size of %(max)d bytes.") % {"max": MAX_ZIP_BYTES})
        try:
            return SkillPackage.inspect(io.BytesIO(uploaded.read()))
        except SkillValidationError as err:
            raise forms.ValidationError(str(err)) from err

    def clean(self):
        cleaned = super().clean()
        # Surface the inspected package on cleaned_data so the view doesn't
        # need to know the form's internal shape.
        if "zip" in cleaned and isinstance(cleaned["zip"], SkillPackage):
            cleaned["package"] = cleaned["zip"]
        return cleaned
