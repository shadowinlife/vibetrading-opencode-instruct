"""Delivery-contract tests for upload_report (RM STS direct upload).

The RM endpoints and OSS are faked at the transport boundary (urlopen,
oss2.StsAuth/Bucket); request construction, headers, digests and secret
hygiene are the real code under test.
"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
import urllib.request
from typing import ClassVar

import oss2
import pytest

from scripts.reports import upload_report

RM_URL = "http://rm.test:8900"
TOKEN = "box-agent-token-SENTINEL"
CREDENTIAL = {
    "report_id": "rp_0123456789abcdef01234567",
    "object_key": "reports/user-1/rp_0123456789abcdef01234567.html",
    "bucket": "rm-reports-test",
    "endpoint": "oss-cn-hangzhou-internal.aliyuncs.com",
    "access_key_id": "STS.ak-id-SENTINEL",
    "access_key_secret": "ak-secret-SENTINEL",
    "security_token": "security-token-SENTINEL",
    "expiration": "2026-11-24T12:00:00Z",
}
SECRETS = [
    TOKEN,
    CREDENTIAL["access_key_id"],
    CREDENTIAL["access_key_secret"],
    CREDENTIAL["security_token"],
]
HTML = "<!DOCTYPE html><html><head><style>body{color:#1c1917}</style></head><body>报告</body></html>"


class FakeResponse:
    def __init__(self, document: dict):
        self._body = json.dumps(document).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingTransport:
    """Routes RM agent endpoints; records every request it sees."""

    def __init__(self, credential_error: Exception | None = None):
        self.requests: list[urllib.request.Request] = []
        self.confirm_bodies: list[dict] = []
        self.credential_error = credential_error

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if request.full_url.endswith("/report-upload-credential"):
            if self.credential_error:
                raise self.credential_error
            return FakeResponse(CREDENTIAL)
        if request.full_url.endswith("/report-uploaded"):
            body = json.loads(request.data.decode("utf-8"))
            self.confirm_bodies.append(body)
            return FakeResponse(
                {
                    "report_id": body["report_id"],
                    "status": "ready",
                    "size_bytes": body["size"],
                }
            )
        raise AssertionError(f"unexpected URL: {request.full_url}")


class RecordingBucket:
    instances: ClassVar[list[RecordingBucket]] = []
    put_error: ClassVar[Exception | None] = None

    def __init__(self, auth, endpoint, bucket):
        self.auth = auth
        self.endpoint = endpoint
        self.bucket = bucket
        self.puts: list[tuple] = []
        RecordingBucket.instances.append(self)

    def put_object(self, key, data, headers=None):
        if RecordingBucket.put_error:
            raise RecordingBucket.put_error
        self.puts.append((key, data, headers))


class RecordingStsAuth:
    def __init__(self, access_key_id, access_key_secret, security_token):
        self.triple = (access_key_id, access_key_secret, security_token)


@pytest.fixture
def transport(monkeypatch):
    RecordingBucket.instances = []
    RecordingBucket.put_error = None
    fake = RecordingTransport()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    monkeypatch.setattr(upload_report.oss2, "StsAuth", RecordingStsAuth)
    monkeypatch.setattr(upload_report.oss2, "Bucket", RecordingBucket)
    return fake


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A sandbox-shaped setup: HTML file + agent token file + RM env."""
    html_path = tmp_path / "report.html"
    html_path.write_text(HTML, encoding="utf-8")
    token_file = tmp_path / "agent-token"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    monkeypatch.setenv(upload_report.RM_URL_ENV, RM_URL)
    return html_path, token_file


def test_full_flow_matches_rm_contract(transport, box):
    html_path, token_file = box
    payload = html_path.read_bytes()

    result = upload_report.upload_report(html_path, RM_URL, token_file)

    credential_req, confirm_req = transport.requests
    assert credential_req.full_url == f"{RM_URL}/api/v1/agent/report-upload-credential"
    assert credential_req.get_method() == "POST"
    assert credential_req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert credential_req.data in (b"", None)

    (bucket,) = RecordingBucket.instances
    assert bucket.auth.triple == (
        CREDENTIAL["access_key_id"],
        CREDENTIAL["access_key_secret"],
        CREDENTIAL["security_token"],
    )
    assert bucket.endpoint == "https://" + CREDENTIAL["endpoint"]
    assert bucket.bucket == CREDENTIAL["bucket"]
    (key, data, headers), = bucket.puts
    assert key == CREDENTIAL["object_key"]
    assert data == payload
    assert headers == {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "private, no-store",
        "Content-Disposition": "inline",
    }

    assert confirm_req.full_url == f"{RM_URL}/api/v1/agent/report-uploaded"
    assert confirm_req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert transport.confirm_bodies == [
        {
            "report_id": CREDENTIAL["report_id"],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
    ]
    assert result == {
        "report_id": CREDENTIAL["report_id"],
        "status": "ready",
        "size_bytes": len(payload),
    }


def test_success_output_carries_no_credentials(transport, box, capsys, monkeypatch):
    html_path, token_file = box
    monkeypatch.setattr(
        "sys.argv", ["upload_report.py", str(html_path), "--token-file", str(token_file)]
    )
    upload_report.main()
    captured = capsys.readouterr()
    assert CREDENTIAL["report_id"] in captured.out
    for secret in SECRETS:
        assert secret not in captured.out + captured.err


@pytest.mark.parametrize(
    "failure",
    [
        "rm-404",
        "rm-502",
        "oss-403",
    ],
)
def test_failure_output_carries_no_credentials(transport, box, capsys, monkeypatch, failure):
    html_path, token_file = box
    if failure == "rm-404":
        transport.credential_error = urllib.error.HTTPError(
            RM_URL, 404, "Not Found", {}, io.BytesIO(b'{"detail": "Not Found"}')
        )
    elif failure == "rm-502":
        transport.credential_error = urllib.error.HTTPError(
            RM_URL,
            502,
            "Bad Gateway",
            {},
            io.BytesIO(b'{"detail": "report storage unavailable"}'),
        )
    else:
        RecordingBucket.put_error = oss2.exceptions.OssError(
            403,
            {},
            b"",
            {
                "Code": "AccessDenied",
                "Message": "Access denied by authorizer's policy.",
                "RequestId": "RID-1",
                "AccessKeyId": CREDENTIAL["access_key_id"],
            },
        )
    monkeypatch.setattr(
        "sys.argv", ["upload_report.py", str(html_path), "--token-file", str(token_file)]
    )
    with pytest.raises(SystemExit) as excinfo:
        upload_report.main()
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error:")
    for secret in SECRETS:
        assert secret not in captured.out + captured.err
    if failure == "oss-403":
        assert "status=403" in captured.err
        assert "code=AccessDenied" in captured.err


@pytest.mark.parametrize(
    "bad_html",
    [
        '<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>',
        '<link rel="stylesheet" href="https://cdn.example.com/style.css">',
        "<style>@font-face{src:url(https://fonts.example.com/f.woff2)}</style>",
    ],
)
def test_external_reference_rejected_before_any_request(transport, box, bad_html):
    html_path, token_file = box
    html_path.write_text(bad_html, encoding="utf-8")
    with pytest.raises(ValueError, match="not self-contained"):
        upload_report.upload_report(html_path, RM_URL, token_file)
    assert transport.requests == []
    assert RecordingBucket.instances == []


def test_missing_token_file_is_clean_error(box, capsys, monkeypatch):
    html_path, _ = box
    monkeypatch.setattr(
        "sys.argv",
        ["upload_report.py", str(html_path), "--token-file", "/nonexistent/agent-token"],
    )
    with pytest.raises(SystemExit) as excinfo:
        upload_report.main()
    assert excinfo.value.code == 1
    assert "Error:" in capsys.readouterr().err


def test_missing_rm_url_is_clean_error(box, capsys, monkeypatch):
    html_path, token_file = box
    monkeypatch.delenv(upload_report.RM_URL_ENV)
    monkeypatch.setattr(
        "sys.argv", ["upload_report.py", str(html_path), "--token-file", str(token_file)]
    )
    with pytest.raises(SystemExit) as excinfo:
        upload_report.main()
    assert excinfo.value.code == 1
    assert upload_report.RM_URL_ENV in capsys.readouterr().err


def test_dry_run_validates_without_network_or_token(transport, box, capsys, monkeypatch):
    html_path, _ = box
    monkeypatch.setattr(
        "sys.argv",
        ["upload_report.py", str(html_path), "--dry-run", "--token-file", "/nonexistent"],
    )
    upload_report.main()
    captured = capsys.readouterr()
    assert "self-contained OK" in captured.out
    assert transport.requests == []


def test_dry_run_rejects_external_script(box, capsys, monkeypatch):
    html_path, _ = box
    html_path.write_text('<script src="https://cdn.example.com/e.js"></script>', encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["upload_report.py", str(html_path), "--dry-run"])
    with pytest.raises(SystemExit) as excinfo:
        upload_report.main()
    assert excinfo.value.code == 1
    assert "not self-contained" in capsys.readouterr().err
