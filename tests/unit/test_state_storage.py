import pytest

from app.core.errors import BadRequest
from app.services import state
from app.storage.base import document_key, validate_key
from app.storage.local import LocalFsStorage
from app.storage.base import StorageError


def test_state_machine_edges():
    assert state.can_transition("queued", "processing")
    assert state.can_transition("processing", "queued")          # retry
    assert state.can_transition("queued", "cancelled")
    assert not state.can_transition("processing", "cancelled")
    for terminal in state.TERMINAL:
        assert all(not state.can_transition(terminal, d) for d in state.ALL)


@pytest.mark.parametrize("bad", ["../x", "/abs", "a/../b", "a//b", "a\\b", "a\x00b", "", "./a"])
def test_storage_key_rejects_traversal(bad):
    with pytest.raises(BadRequest):
        validate_key(bad)


def test_document_key_is_server_generated_shape():
    assert document_key("cli_1", "proc_2", "png") == "tenants/cli_1/documents/proc_2/original.png"


def test_local_storage_roundtrip_and_escape(tmp_path):
    s = LocalFsStorage(str(tmp_path / "o"))
    s.put("a/b.bin", b"hi", "x"); assert s.get("a/b.bin") == b"hi"
    s.delete("a/b.bin")
    with pytest.raises(Exception):
        s.get("a/b.bin")
    with pytest.raises(BadRequest):
        s.put("../escape", b"x", "x")
