"""Tests for vision/image attachments: encoding, multimodal content, and
the vision-capability heuristic. Clipboard paste is platform-shelled and
covered only via subprocess mocking (no real clipboard in CI)."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch

import pytest

from wells import vision


_PNG_BYTES = base64.b64decode(
    # Smallest valid 1x1 transparent PNG.
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


@pytest.fixture
def png_path(tmp_path: Path) -> Path:
    p = tmp_path / "screenshot.png"
    p.write_bytes(_PNG_BYTES)
    return p


# ---------------------------------------------------------------------------
# encode_image_file
# ---------------------------------------------------------------------------


def test_encode_image_file_returns_data_url_block(png_path: Path):
    block = vision.encode_image_file(str(png_path))
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")
    decoded = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
    assert decoded == _PNG_BYTES


def test_encode_image_file_missing_raises(tmp_path: Path):
    with pytest.raises(vision.VisionError, match="not found"):
        vision.encode_image_file(str(tmp_path / "nope.png"))


def test_encode_image_file_rejects_unsupported_extension(tmp_path: Path):
    bad = tmp_path / "doc.pdf"
    bad.write_bytes(b"%PDF-1.4")
    with pytest.raises(vision.VisionError, match="[Uu]nsupported"):
        vision.encode_image_file(str(bad))


def test_encode_image_file_rejects_oversized(tmp_path: Path, monkeypatch):
    p = tmp_path / "big.png"
    p.write_bytes(_PNG_BYTES)
    monkeypatch.setattr(vision, "_MAX_IMAGE_BYTES", 4)  # smaller than the fixture
    with pytest.raises(vision.VisionError, match="too large"):
        vision.encode_image_file(str(p))


# ---------------------------------------------------------------------------
# build_multimodal_content
# ---------------------------------------------------------------------------


def test_build_multimodal_content_plain_text_without_images():
    out = vision.build_multimodal_content("do the thing", [])
    assert out == "do the thing"  # NOT wrapped in a list — byte-identical to before


def test_build_multimodal_content_with_images(png_path: Path):
    out = vision.build_multimodal_content("look at this", [str(png_path)])
    assert isinstance(out, list)
    assert out[0] == {"type": "text", "text": "look at this"}
    assert out[1]["type"] == "image_url"


def test_build_multimodal_content_multiple_images(png_path: Path, tmp_path: Path):
    second = tmp_path / "second.png"
    second.write_bytes(_PNG_BYTES)
    out = vision.build_multimodal_content("compare these", [str(png_path), str(second)])
    assert len(out) == 3  # text + 2 images


def test_build_multimodal_content_raises_on_bad_path():
    with pytest.raises(vision.VisionError):
        vision.build_multimodal_content("x", ["/definitely/not/a/real/path.png"])


# ---------------------------------------------------------------------------
# provider_supports_vision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", [
    "anthropic:claude-fable-5", "openai:gpt-4o", "openai:gpt-5.2",
    "google:gemini-2.5-pro", "ollama:llava:13b", "ollama:qwen2.5-vl:7b",
])
def test_provider_supports_vision_true_for_known_vision_models(label):
    assert vision.provider_supports_vision(label) is True


@pytest.mark.parametrize("label", [
    "zai:glm-5.2", "ollama:qwen2.5-coder:7b", "deepseek:deepseek-reasoner", "",
])
def test_provider_supports_vision_false_for_non_vision_models(label):
    assert vision.provider_supports_vision(label) is False


# ---------------------------------------------------------------------------
# paste_clipboard_image (subprocess mocked — no real clipboard in CI)
# ---------------------------------------------------------------------------


def test_paste_clipboard_image_returns_none_on_empty_clipboard(monkeypatch):
    monkeypatch.setattr(vision.platform, "system", lambda: "Windows")

    class _Proc:
        returncode = 1

    with patch.object(vision.subprocess, "run", return_value=_Proc()):
        assert vision.paste_clipboard_image() is None


def test_paste_clipboard_image_never_raises_on_tool_missing(monkeypatch):
    monkeypatch.setattr(vision.platform, "system", lambda: "Linux")
    with patch.object(vision.subprocess, "run", side_effect=FileNotFoundError("no wl-paste")):
        assert vision.paste_clipboard_image() is None  # must not raise
