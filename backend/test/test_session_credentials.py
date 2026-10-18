import subprocess
import sys

from app.application.session.credentials import CredentialFilter


def test_credential_filter_nested_values_and_stream_tail():
    secret = "credential-boundary-value"
    guard = CredentialFilter(("", secret))
    assert guard.contains({"nested": [{secret: "value"}]})
    assert not guard.contains({"nested": [None, 1, "safe"]})
    for index in range(len(secret) + 1):
        prefix = secret[:index]
        assert guard.contains(prefix) == (index == len(secret))
        assert secret not in guard.tail_safe(prefix)
    assert CredentialFilter(()).tail_safe("正文") == "正文"


def test_credentials_import_is_independent_of_http():
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; "
            "from app.application.session.credentials import CredentialFilter; "
            "assert CredentialFilter(('secret',)).contains('secret'); "
            "assert not any(name.startswith(('app.interfaces', 'fastapi', 'starlette')) "
            "for name in sys.modules)"
        )],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
