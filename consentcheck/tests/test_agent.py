from app.agent import ProviderError, ToolCall, Turn, run_agent


class FakeProvider:
    """Test-only scripted provider; production never imports this module."""
    def __init__(self, turns):
        self.turns = iter(turns)
        self.results = []

    def start(self, task):
        self.task = task

    def next_turn(self):
        turn = next(self.turns)
        if isinstance(turn, Exception):
            raise turn
        return turn

    def record_results(self, turn, results):
        self.results.append(results)

    def close(self):
        pass


def successful_turns():
    return [
        Turn([ToolCall("search_documents", {"query": "quarterly"})]),
        Turn([ToolCall("read_document", {"document_id": "quarterly-report-q3"})]),
        Turn([ToolCall("propose_send_document", {"document_id": "quarterly-report-q3", "recipient": "Alice"})]),
        Turn([], text="I sent the document!"),
    ]


def test_success_executes_actual_validated_tools_and_ignores_false_sent_claim():
    provider = FakeProvider(successful_turns())
    result = run_agent("Find quarterly report for Alice", provider)
    assert result["status"] == "completed"
    assert len(result["activity"]) == 3
    assert result["proposals"][0]["recipient"] == "Alice"
    assert "revenue" in provider.results[1][0]["content"]
    assert "I sent" not in result["message"]
    assert result["sent"] is False


def test_tool_errors_are_returned_to_provider():
    provider = FakeProvider([Turn([ToolCall("shell", {"command": "anything"})]), Turn([])])
    assert run_agent("test", provider)["proposals"] == []
    assert "error" in provider.results[0][0]


def test_limits_and_partial_error_activity():
    turn = Turn([ToolCall("search_documents", {"query": "report"})])
    assert run_agent("test", FakeProvider([turn] * 8))["status"] == "limit_reached"
    result = run_agent("test", FakeProvider([turn, ProviderError("Quota reached")]))
    assert result["status"] == "error"
    assert len(result["activity"]) == 1
    assert result["message"] == "Quota reached"
    assert run_agent("test", FakeProvider([Turn(turn.calls * 25)]))["activity"] == []
