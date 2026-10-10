import pytest

from app.documents import read_document, search_documents
from app.tools import ToolRunner


def test_document_search_and_read():
    matches = search_documents("QUARTERLY report")["documents"]
    assert matches[0]["document_id"] == "quarterly-report-q3"
    assert "revenue" in read_document(matches[0]["document_id"])["content"]
    assert search_documents("no-such-document")["documents"] == []
    with pytest.raises(ValueError):
        read_document("../../.env")


@pytest.mark.parametrize("name,args", [
    ("send_email", {}),
    ("search_documents", {"query": " "}),
    ("search_documents", {"query": 123}),
    ("search_documents", {"query": "report", "shell": "anything"}),
    ("read_document", {"document_id": "unknown"}),
    ("propose_send_document", {"document_id": "quarterly-report-q3", "recipient": "Alice"}),
])
def test_rejects_invalid_tools(name, args):
    assert "error" in ToolRunner().execute(name, args)


def test_proposals_are_only_in_memory_and_require_read_per_run():
    runner = ToolRunner()
    runner.execute("read_document", {"document_id": "quarterly-report-q3"})
    arguments = {"document_id": "quarterly-report-q3", "recipient": "Alice"}
    assert runner.execute("propose_send_document", arguments)["sent"] is False
    assert len(runner.proposals) == 1
    assert "error" in ToolRunner().execute("propose_send_document", arguments)
    arguments["recipient"] = "Alice\nBcc: Bob"
    assert "error" in runner.execute("propose_send_document", arguments)
