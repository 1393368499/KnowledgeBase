from django_otp.admin import OTPAdminSite


class MfaAdminSite(OTPAdminSite):
    def __init__(self, name="admin"):
        super().__init__(name)
