"""Tests for zakuro.cli.prompts — stdlib-only prompt helpers."""

import io

import pytest

from zakuro.cli import prompts


class TestAsk:
    def test_returns_input(self) -> None:
        stream = io.StringIO("hello\n")
        assert prompts.ask("name?", input_stream=stream) == "hello"

    def test_empty_returns_default(self) -> None:
        stream = io.StringIO("\n")
        assert prompts.ask("name?", default="bob", input_stream=stream) == "bob"

    def test_no_default_empty_raises(self) -> None:
        stream = io.StringIO("\n")
        with pytest.raises(ValueError, match="required"):
            prompts.ask("name?", input_stream=stream)


class TestAskChoice:
    def test_picks_first(self) -> None:
        stream = io.StringIO("1\n")
        assert prompts.ask_choice("mode?", ["local", "distributed"], input_stream=stream) == "local"

    def test_picks_second(self) -> None:
        stream = io.StringIO("2\n")
        assert (
            prompts.ask_choice("mode?", ["local", "distributed"], input_stream=stream)
            == "distributed"
        )

    def test_invalid_then_valid(self) -> None:
        stream = io.StringIO("9\nfoo\n2\n")
        assert (
            prompts.ask_choice("mode?", ["local", "distributed"], input_stream=stream)
            == "distributed"
        )

    def test_default_on_empty(self) -> None:
        stream = io.StringIO("\n")
        assert (
            prompts.ask_choice(
                "mode?", ["local", "distributed"], default="distributed", input_stream=stream
            )
            == "distributed"
        )

    def test_eof_without_default_raises(self) -> None:
        stream = io.StringIO("")  # immediate EOF
        with pytest.raises(EOFError):
            prompts.ask_choice("mode?", ["local", "distributed"], input_stream=stream)


class TestConfirm:
    @pytest.mark.parametrize(
        "answer,expected",
        [("y", True), ("Y", True), ("yes", True), ("n", False), ("N", False), ("no", False)],
    )
    def test_explicit(self, answer: str, expected: bool) -> None:
        stream = io.StringIO(f"{answer}\n")
        assert prompts.confirm("ok?", input_stream=stream) is expected

    def test_default_true(self) -> None:
        stream = io.StringIO("\n")
        assert prompts.confirm("ok?", default=True, input_stream=stream) is True

    def test_default_false(self) -> None:
        stream = io.StringIO("\n")
        assert prompts.confirm("ok?", default=False, input_stream=stream) is False


class TestSecret:
    def test_reads_from_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # getpass falls back to reading from stdin when echo can't be suppressed
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "zk_supersecret")
        assert prompts.secret("api key?") == "zk_supersecret"


class TestIsInteractive:
    def test_false_for_pipe(self) -> None:
        stream = io.StringIO("anything")
        assert prompts.is_interactive(stream) is False
