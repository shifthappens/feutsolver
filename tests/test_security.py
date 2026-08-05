from pathlib import Path
import logging

import pytest
from PIL import Image

import wordfeud_analyzer.move_generator as move_generator
from wordfeud_analyzer.move_generator import (
    _cache_path,
    add_words_to_wordlist,
    load_wordlist,
    remove_words_from_wordlist,
)
from wordfeud_analyzer.security import (
    OCRBudget,
    ResourceLimitError,
    SlidingWindowRateLimiter,
    authenticated_actor,
    may_mutate_production_wordlist,
    log_security_event,
    redact_text,
)
from wordfeud_analyzer.vision import ImageValidationError, validate_and_normalize_image


def test_identity_is_fail_closed_for_missing_duplicate_or_spoofed_headers() -> None:
    assert authenticated_actor(None) is None
    assert authenticated_actor({}) is None
    assert authenticated_actor({"X-Authenticated-User": "feutsolver", "x-authenticated-user": "other"}) is None
    assert authenticated_actor({"X-Authenticated-User": "feutsolver", "X-Forwarded-User": "feutsolver"}) is None
    assert authenticated_actor({"X-Authenticated-User": " feutsolver"}) is None
    assert authenticated_actor({"X-Authenticated-User": "feutsolver"}) == "feutsolver"

    class RepeatedHeaders(dict[str, str]):
        def get_all(self, name: str) -> list[str]:
            if name.casefold() == "x-authenticated-user":
                return ["feutsolver", "other-user"]
            return []

    assert authenticated_actor(RepeatedHeaders({"x-authenticated-user": "feutsolver"})) is None


def test_only_the_exact_production_actor_can_mutate() -> None:
    assert may_mutate_production_wordlist("feutsolver") is True
    assert may_mutate_production_wordlist("Feutsolver") is False
    assert may_mutate_production_wordlist("other-user") is False
    assert may_mutate_production_wordlist(None) is False


def test_wordlist_mutations_deny_non_owner_server_side(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    source = tmp_path / "words.txt"
    source.write_text("kat\ngin\n", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="feutsolver.security"):
        with pytest.raises(PermissionError, match="WORDLIST-DENIED"):
            add_words_to_wordlist(["nieuw"], source, actor="other-user")
        with pytest.raises(PermissionError, match="WORDLIST-DENIED"):
            remove_words_from_wordlist(["gin"], source, actor=None)
    assert source.read_text(encoding="utf-8") == "kat\ngin\n"
    assert sum('"event":"wordlist_mutation"' in record.message for record in caplog.records) == 2


def test_upload_with_tiny_bytes_but_too_many_pixels_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "not-an-image.jpg"
    destination = tmp_path / "normalized.png"
    with Image.new("RGB", (8_001, 1), "white") as image:
        image.save(source, format="PNG")

    with pytest.raises(ImageValidationError, match="dimensions") as error:
        validate_and_normalize_image(source, destination)
    assert error.value.code == "IMG-DIMENSIONS"
    assert not destination.exists()


def test_corrupt_and_mislabeled_formats_are_rejected_without_crashing(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"not a decoder-valid image")
    with pytest.raises(ImageValidationError) as error:
        validate_and_normalize_image(corrupt, tmp_path / "corrupt-normalized.png")
    assert error.value.code in {"IMG-CORRUPT", "IMG-FORMAT"}

    gif = tmp_path / "supported-by-extension.png"
    with Image.new("P", (4, 4), 0) as image:
        image.save(gif, format="GIF")
    with pytest.raises(ImageValidationError) as error:
        validate_and_normalize_image(gif, tmp_path / "gif-normalized.png")
    assert error.value.code == "IMG-FORMAT"


def test_normalization_uses_the_decoded_format_and_strips_to_rgb(tmp_path: Path) -> None:
    source = tmp_path / "actually-png.jpg"
    destination = tmp_path / "normalized.png"
    with Image.new("RGBA", (12, 8), (20, 40, 60, 128)) as image:
        image.save(source, format="PNG")

    result = validate_and_normalize_image(source, destination)
    assert result == destination
    with Image.open(destination) as normalized:
        assert normalized.format == "PNG"
        assert normalized.mode == "RGB"
        assert normalized.size == (12, 8)


def test_ocr_budget_and_external_rate_limit_are_bounded() -> None:
    budget = OCRBudget.start(timeout_seconds=5, max_attempts=2)
    budget.consume_attempt()
    budget.consume_attempt()
    with pytest.raises(ResourceLimitError, match="attempt budget"):
        budget.consume_attempt()

    limiter = SlidingWindowRateLimiter(limit=2, window_seconds=10, max_keys=2)
    assert limiter.allow("actor", now=100) is True
    assert limiter.allow("actor", now=101) is True
    assert limiter.allow("actor", now=102) is False
    assert limiter.allow("actor", now=111) is True


def test_security_logs_have_timestamp_and_redact_sensitive_detail_keys(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="feutsolver.security"):
        log_security_event(
            event="test",
            correlation_id="corr",
            authorization="Bearer secret",
            cookie="session-cookie",
            error_reason="Authorization Bearer secret api_key=placeholder-key",
            safe_count=2,
        )
    record = caplog.records[-1].message
    assert '"timestamp":' in record
    assert "secret" not in record
    assert "session-cookie" not in record
    assert '"safe_count":2' in record
    assert redact_text("Bearer secret api_key=placeholder-key") == "Bearer [redacted] api_key=[redacted]"


def test_security_logger_is_verbose_without_promoting_library_loggers() -> None:
    assert logging.getLogger().level == logging.WARNING
    assert logging.getLogger("feutsolver.security").level == logging.INFO


def test_tampered_json_cache_rebuilds_and_source_changes_invalidate_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(move_generator, "RUNTIME_CACHE_WRITE", True)
    source = tmp_path / "words.txt"
    source.write_text("kat\n", encoding="utf-8")
    first = load_wordlist(source)
    cache = _cache_path(source)
    assert first.contains("KAT")
    cache.write_text('{"payload":"tampered"}', encoding="utf-8")

    rebuilt = load_wordlist(source)
    assert rebuilt.contains("KAT")
    assert cache.read_text(encoding="utf-8").startswith('{"magic":"FEUTSOLVER-GADDAG"')

    source.write_text("kat\ngin\n", encoding="utf-8")
    changed = load_wordlist(source)
    assert changed.contains("GIN")
