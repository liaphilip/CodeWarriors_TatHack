"""Synthetic, in-memory documents only; IDs never become filesystem paths."""
DOCUMENTS = {
    "quarterly-report-q3": {
        "title": "Quarterly Report — Q3 2026",
        "content": "Synthetic Q3 2026 report: revenue $120,000; expenses $85,000; "
                   "35 new customers. Next quarter: improve onboarding and retention.",
    },
    "project-roadmap": {
        "title": "Product Roadmap",
        "content": "Synthetic roadmap: October onboarding, November analytics, December accessibility.",
    },
    "team-handbook": {
        "title": "Team Handbook",
        "content": "Synthetic handbook: document decisions and review sensitive actions before execution.",
    },
}


def search_documents(query: str) -> dict:
    terms = query.casefold().split()
    return {"documents": [
        {"document_id": key, "title": doc["title"]}
        for key, doc in DOCUMENTS.items()
        if any(term in (doc["title"] + " " + doc["content"]).casefold() for term in terms)
    ]}


def read_document(document_id: str) -> dict:
    if document_id not in DOCUMENTS:
        raise ValueError("Unknown document ID. Search for an available document first.")
    return {"document_id": document_id, **DOCUMENTS[document_id]}
