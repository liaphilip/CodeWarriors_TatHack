"""The entire agent tool allowlist. No sending or arbitrary execution tool exists."""
from pydantic import ValidationError

from .documents import read_document, search_documents
from .schemas import ProposeInput, ReadInput, SearchInput

MODELS = {"search_documents": SearchInput, "read_document": ReadInput,
          "propose_send_document": ProposeInput}
DESCRIPTIONS = {
    "search_documents": "Find synthetic documents by keywords; returns IDs and titles.",
    "read_document": "Read a synthetic document by its ID before proposing a send.",
    "propose_send_document": "Propose sending a previously read document to a recipient name or email. Never sends.",
}


def tool_declarations() -> list[dict]:
    return [{"name": name, "description": DESCRIPTIONS[name],
             "parameters_json_schema": model.model_json_schema()}
            for name, model in MODELS.items()]


class ToolRunner:
    def __init__(self) -> None:
        self.read_ids: set[str] = set()
        self.proposals: list[dict] = []

    def execute(self, name: str, arguments: dict) -> dict:
        if name not in MODELS:
            return {"error": "Unknown tool. Only document search, read, and proposal tools are available."}
        try:
            data = MODELS[name].model_validate(arguments)
            if name == "search_documents":
                return search_documents(data.query)
            if name == "read_document":
                result = read_document(data.document_id)
                self.read_ids.add(data.document_id)
                return result
            if data.document_id not in self.read_ids:
                return {"error": "Read this document before proposing to send it."}
            if any(ord(char) < 32 for char in data.recipient):
                return {"error": "Recipient must not contain control characters."}
            proposal = {"document_id": data.document_id,
                        "title": read_document(data.document_id)["title"],
                        "recipient": data.recipient, "status": "proposed", "sent": False,
                        "action_type": "send_document",
                        "notice": "Approval performs a simulated send only. No real email is sent."}
            proposal.update(cc=data.cc, bcc=data.bcc,
                            subject=data.subject if data.subject is not None else proposal['title'],
                            body=data.body if data.body is not None else f"Please find attached: {proposal['title']}.")
            self.proposals.append(proposal)
            return proposal
        except ValidationError:
            return {"error": "Invalid tool arguments: check required fields, types, lengths, and extra fields."}
        except ValueError as exc:
            return {"error": str(exc)}
