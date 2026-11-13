import pytest
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice


@pytest.mark.django_db
def test_admin_requires_totp_and_rejects_replay(client, django_user_model):
    user = django_user_model.objects.create_superuser("mfa-admin", password="admin-test-password")
    device = TOTPDevice.objects.create(user=user, name="test")
    client.force_login(user)
    assert client.get("/admin/").status_code == 302
    client.logout()

    login = {"username": user.username, "password": "admin-test-password", "next": "/admin/"}
    assert client.post("/admin/login/", login).status_code == 200
    assert client.get("/admin/").status_code == 302

    code = f"{totp(device.bin_key):06d}"
    verified_login = {**login, "otp_device": device.persistent_id, "otp_token": code}
    assert client.post("/admin/login/", verified_login).status_code == 302
    assert client.get("/admin/").status_code == 200
    client.logout()

    assert client.post("/admin/login/", verified_login).status_code == 200
    assert client.get("/admin/").status_code == 302
