import base64
import os
import stat
import subprocess
from functools import partial
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.application.session.attachment_files import AttachmentFiles, PreparedUpload
from app.application.session.service import _reject_credentials
from app.domain.session.attachments import (
    AttachmentError,
    AttachmentMetadata,
    attachment_storage_ref,
)
from app.domain.session.errors import CredentialDetected

ROOT = Path(__file__).resolve().parents[2]


def upload(data: bytes = b"plan", file_name: str = "plan.md", attachment_id: str | None = None) -> dict:
    return {
        "kind": "upload",
        "attachment_id": attachment_id or str(uuid4()),
        "file_name": file_name,
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def expect_code(code: str, action) -> None:
    with pytest.raises(AttachmentError) as failure:
        action()
    assert failure.value.code == code


def main(*, fixture_root: Path | None = None) -> None:
    root = (fixture_root or ROOT / "tmp" / "attachment-check") / str(uuid4())
    root.mkdir(parents=True)
    check = partial(_reject_credentials, credentials=("protected-test-credential",))
    files = AttachmentFiles(root, check_credentials=check)
    session = str(uuid4())
    original = "# 训练\r\n深蹲\n\ufeff".encode("utf-8")
    items = [upload(original, "计划.MD"), upload(b"\xef\xbb\xbftext\r\n", "notes.TxT")]
    prepared = files.prepare(session, items, existing={})
    metadata = [files.write(session, item, created_at=1) for item in prepared]
    for item, stored in zip(items, metadata, strict=True):
        data = base64.b64decode(item["data_base64"], validate=True)
        assert (root / stored.storage_ref).read_bytes() == data
        assert files.read(session, stored).encode("utf-8") == data
        assert stored.file_name == item["file_name"]
    workspace = root / "tmp" / "sessions" / session / "workspace"
    workspace.mkdir()
    (workspace / "work.txt").write_bytes(b"work")
    restarted = AttachmentFiles(root, check_credentials=check)
    assert restarted.read(session, metadata[0]).encode("utf-8") == original
    assert (workspace / "work.txt").read_bytes() == b"work"

    exact = files.prepare(session, [upload(b"x" * 100000)], existing={})
    boundary = files.write(session, exact[0], created_at=2)
    assert len(files.read(session, boundary).encode("utf-8")) == 100000
    expect_code("attachment_size_exceeded", lambda: files.prepare(session, [upload(b"x" * 100001)], existing={}))
    references = {item.attachment_id: item for item in metadata}
    reference = {"kind": "reference", "attachment_id": metadata[0].attachment_id}
    assert files.prepare(session, [reference], existing=references) == (metadata[0],)
    files.prepare(session, [reference, upload(b"x" * (100000 - len(original)))], existing=references)
    expect_code("attachment_size_exceeded", lambda: files.prepare(session, [reference, upload(b"x" * (100001 - len(original)))], existing=references))
    expect_code("attachment_access_denied", lambda: files.prepare(str(uuid4()), [reference], existing=references))
    expect_code("attachment_access_denied", lambda: files.read(str(uuid4()), metadata[0]))
    expect_code("attachment_not_found", lambda: files.prepare(session, [reference], existing={}))
    expect_code("invalid_request", lambda: files.prepare(session, [reference, reference], existing=references))
    duplicate = upload(attachment_id=metadata[0].attachment_id)
    expect_code("attachment_conflict", lambda: files.prepare(session, [duplicate], existing=references))
    expect_code("attachment_conflict", lambda: files.write(session, prepared[0], created_at=1))
    opposite = PreparedUpload(metadata[0].attachment_id, "other.txt", b"other")
    expect_code("attachment_conflict", lambda: files.write(session, opposite, created_at=1))
    assert (root / metadata[0].storage_ref).read_bytes() == original

    for name in ("", "../x.md", "a/b.md", "a\\b.txt", "a\x00.md", "C:x.md", "C:\\x.txt", "..", "/x.md", "x.md:stream"):
        with pytest.raises(ValidationError):
            files.prepare(session, [upload(file_name=name)], existing={})
    with pytest.raises(ValidationError) as invalid_extension:
        files.prepare(session, [upload(file_name="plan.pdf")], existing={})
    assert invalid_extension.value.errors()[0]["type"] == "attachment_format_invalid"
    for invalid in ("!", "abc", "YW Jj", "____", "é"):
        item = upload()
        item["data_base64"] = invalid
        with pytest.raises(ValidationError):
            files.prepare(session, [item], existing={})
    with pytest.raises(ValidationError):
        files.prepare(session, [upload(b"\xff")], existing={})
    for field, value in (("kind", "bad"), ("attachment_id", "invalid"), ("attachment_id", uuid4()), ("file_name", 1), ("data_base64", True), ("extra", 1)):
        item = upload()
        item[field] = value
        with pytest.raises(ValidationError):
            files.prepare(session, [item], existing={})
    for field in upload():
        item = upload()
        del item[field]
        with pytest.raises(ValidationError):
            files.prepare(session, [item], existing={})
    for item in ({"kind": "reference"}, {"kind": "reference", "attachment_id": False}, {**reference, "extra": 1}):
        with pytest.raises(ValidationError):
            files.prepare(session, [item], existing=references)
    with pytest.raises(ValidationError):
        files.prepare(session, tuple(items), existing={})
    upper = upload()
    upper["attachment_id"] = upper["attachment_id"].upper()
    lower = {**upper, "attachment_id": upper["attachment_id"].lower()}
    expect_code("invalid_request", lambda: files.prepare(session, [upper, lower], existing={}))
    for item in (upload(b"protected-test-credential"), upload(file_name="protected-test-credential.md")):
        with pytest.raises(CredentialDetected):
            files.prepare(session, [item], existing={})
    with pytest.raises(CredentialDetected):
        files.write(session, PreparedUpload(str(uuid4()), "plan.md", b"protected-test-credential"), created_at=1)

    damaged = metadata[1]
    damaged_path = root / damaged.storage_ref
    damaged_path.chmod(stat.S_IWRITE | stat.S_IREAD)
    damaged_path.write_bytes(b"bad")
    expect_code("internal_error", lambda: files.read(session, damaged))
    damaged_path.write_bytes(b"\xff" * damaged.size_bytes)
    with pytest.raises(UnicodeDecodeError):
        files.read(session, damaged)
    damaged_path.unlink()
    expect_code("internal_error", lambda: files.read(session, damaged))
    for storage_ref in ("../outside.md", "/absolute.md", metadata[1].storage_ref):
        invalid = metadata[0].model_copy(update={"storage_ref": storage_ref})
        expect_code("internal_error", lambda: files.read(session, invalid))
    mismatched = {metadata[0].attachment_id: metadata[1]}
    expect_code("internal_error", lambda: files.prepare(session, [reference], existing=mismatched))

    created = []
    owned_upload = PreparedUpload(str(uuid4()), "owned.txt", b"owned")
    owned = files.write(session, owned_upload, created_at=3, on_created=created.append)
    assert len(created) == 1
    with pytest.raises(AttachmentError):
        files.write(session, owned_upload, created_at=3, on_created=created.append)
    assert len(created) == 1
    files.remove_created(created[0])
    assert not (root / owned.storage_ref).exists()
    replaced = []
    stored = files.write(session, PreparedUpload(str(uuid4()), "identity.md", b"identity"), created_at=3, on_created=replaced.append)
    stored_path = root / stored.storage_ref
    stored_path.chmod(stat.S_IWRITE | stat.S_IREAD)
    retained_path = stored_path.with_suffix(".retained")
    stored_path.rename(retained_path)
    stored_path.write_bytes(b"replacement")
    expect_code("internal_error", lambda: files.remove_created(replaced[0]))
    assert stored_path.read_bytes() == b"replacement"
    assert retained_path.read_bytes() == b"identity"
    if os.name == "nt":
        import msvcrt

        locked = []
        handles = []
        lock_upload = PreparedUpload(str(uuid4()), "locked.txt", b"locked production flush")
        lock_path = root / attachment_storage_ref(session, lock_upload.attachment_id, lock_upload.file_name)

        def lock_created(token):
            locked.append(token)
            handle = lock_path.open("r+b")
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            handles.append(handle)

        with pytest.raises(OSError):
            files.write(session, lock_upload, created_at=3, on_created=lock_created)
        assert len(locked) == 1 and len(handles) == 1
        msvcrt.locking(handles[0].fileno(), msvcrt.LK_UNLCK, 1)
        handles[0].close()
        files.remove_created(locked[0])
        assert not lock_path.exists()
        print("Real Windows byte lock: production write/flush rejected; owned file cleaned")

    outside = root / "outside"
    outside.mkdir()
    linked_session = str(uuid4())
    linked_parent = root / "tmp" / "sessions" / linked_session
    linked_parent.mkdir()
    link = linked_parent / "attachments"
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr.decode(errors="strict")
        print("Windows junction escape: rejected")
    else:
        link.symlink_to(outside, target_is_directory=True)
        print("Directory symlink escape: rejected")
    expect_code("internal_error", lambda: files.write(linked_session, PreparedUpload(str(uuid4()), "plan.md", b"text"), created_at=1))
    expect_code("internal_error", lambda: AttachmentFiles(link, check_credentials=check))
    linked_id = str(uuid4())
    linked_metadata = AttachmentMetadata(
        attachment_id=linked_id,
        session_id=linked_session,
        file_name="plan.md",
        size_bytes=4,
        storage_ref=attachment_storage_ref(linked_session, linked_id, "plan.md"),
        created_at=1,
    )
    expect_code("internal_error", lambda: files.read(linked_session, linked_metadata))
    assert list(outside.iterdir()) == []
    from app.application.session.attachment_files import CreatedAttachment

    escaped = CreatedAttachment(files.project_root, linked_session, linked_id, "plan.md", 0, 0)
    expect_code("internal_error", lambda: files.remove_created(escaped))
    assert list(outside.iterdir()) == []
    print("Attachment checks passed: strict wire, original bytes, size boundaries, references, credentials, conflicts, corruption, path escape, restart retention")
    print(f"Real fixtures: {root}")


if __name__ == "__main__":
    main()
