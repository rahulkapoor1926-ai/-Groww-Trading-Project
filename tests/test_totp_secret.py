import pytest

from app.groww_client import GrowwConfigError, GrowwSession, normalize_totp_secret

SECRET = "JBSWY3DPEHPK3PXP"


@pytest.mark.parametrize(
    "raw",
    [
        SECRET,
        " jbswy3dpehpk3pxp \n",
        "JBSW Y3DP EHPK 3PXP",
        "JBSW-Y3DP-EHPK-3PXP",
        "JBSWY3DP\nEHPK3PXP",
        '"JBSWY3DPEHPK3PXP"',
        "'JBSWY3DPEHPK3PXP'",
        "otpauth://totp/Groww:me?secret=JBSWY3DPEHPK3PXP&issuer=Groww",
    ],
)
def test_normalizes_common_paste_formats(raw):
    assert normalize_totp_secret(raw) == SECRET


@pytest.mark.parametrize(
    "raw,hint",
    [
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc_def", "swapped"),
        ("JBSWY3DPEHPK3PX0", "0, 1, 8 or 9"),
        ("JBSWY3DP.EHPK3PXP", "symbols"),
        ("   ", "empty"),
    ],
)
def test_bad_secrets_explain_without_leaking(raw, hint):
    with pytest.raises(GrowwConfigError) as exc:
        normalize_totp_secret(raw)
    msg = str(exc.value)
    assert hint in msg
    assert "GROWW_TOTP_SECRET" in msg
    if len(raw.strip()) > 4:
        assert raw.strip() not in msg


def test_swapped_api_key_is_detected():
    s = GrowwSession(api_key=SECRET, totp_secret="eyJabc.def")
    with pytest.raises(
        GrowwConfigError, match="GROWW_API_KEY looks like a TOTP secret"
    ):
        s.login()
