"""Vision: attach images (file paths or clipboard paste) to a task so a
vision-capable model can actually see them — a screenshot of a UI bug, an
error dialog, a design mock, a whiteboard photo of an architecture sketch.

Content blocks use the ``{"type": "image_url", "image_url": {"url": "data:..."}}``
shape, the de-facto standard LangChain's chat-model integrations normalize
per provider (ChatOpenAI, ChatAnthropic, ChatGoogleGenerativeAI, and
ChatOllama against a vision model like llava/qwen2.5-vl all accept it), so
one encoding path works across every provider without per-provider branches.

Clipboard paste is OS-specific (terminals don't expose raw pasted image
bytes to a TUI the way a browser does) — each platform's own screenshot/
clipboard tool is shelled out to, writing a temp PNG that's then encoded
the same way as a file path.
"""

from __future__ import annotations

import base64
import mimetypes
import platform
import subprocess
import tempfile
from pathlib import Path

_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20MB — generous; providers cap lower anyway
_ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class VisionError(Exception):
    """Raised for a user-facing image problem (bad path, too large, unsupported type)."""


def encode_image_file(path: str) -> dict:
    """Return an ``image_url`` content block for ``path``. Raises VisionError."""
    p = Path(path)
    if not p.is_file():
        raise VisionError(f"Image not found: {path}")
    ext = p.suffix.lower()
    if ext not in _ALLOWED_EXTS:
        raise VisionError(
            f"Unsupported image type {ext!r} for {path} — "
            f"supported: {', '.join(sorted(_ALLOWED_EXTS))}"
        )
    size = p.stat().st_size
    if size > _MAX_IMAGE_BYTES:
        raise VisionError(
            f"Image too large ({size / 1024 / 1024:.1f}MB > "
            f"{_MAX_IMAGE_BYTES / 1024 / 1024:.0f}MB limit): {path}"
        )
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def build_multimodal_content(text: str, image_paths: list[str]) -> list[dict] | str:
    """Content for a HumanMessage: multimodal list if images are given, else plain text.

    Returning plain ``str`` (not a one-item list) when there are no images
    keeps every existing text-only call site's behavior byte-identical —
    multimodal content blocks are opt-in, never a silent format change for
    callers that never pass images.

    Raises VisionError on the first bad image path — fail loudly before a
    task starts rather than silently sending a truncated/broken attachment
    set to the model.
    """
    if not image_paths:
        return text
    blocks: list[dict] = [{"type": "text", "text": text}]
    for path in image_paths:
        blocks.append(encode_image_file(path))
    return blocks


# ---------------------------------------------------------------------------
# Vision-capability heuristic
# ---------------------------------------------------------------------------

_VISION_MODEL_MARKERS = (
    "claude-", "gpt-4o", "gpt-5", "o3", "o4", "gemini-", "llava", "-vl",
    "vision", "pixtral", "qwen2.5-vl", "qwen2-vl",
)


def provider_supports_vision(model_label: str) -> bool:
    """Heuristic: does ``model_label`` (e.g. 'zai:glm-5.2') look vision-capable?

    Best-effort only — used to warn, not to block. False negatives (a real
    vision model whose name doesn't match) just mean a missed warning, not a
    broken run; the harness never refuses to send images based on this.
    """
    label = (model_label or "").lower()
    return any(m in label for m in _VISION_MODEL_MARKERS)


# ---------------------------------------------------------------------------
# Clipboard paste (OS-specific)
# ---------------------------------------------------------------------------


def paste_clipboard_image() -> Path | None:
    """Save the system clipboard's image (if any) to a temp PNG; return its path.

    None when the clipboard has no image, the platform tool is unavailable,
    or anything goes wrong — never raises, since "nothing to paste" is a
    routine, expected outcome, not an error.
    """
    system = platform.system()
    out = Path(tempfile.gettempdir()) / f"wells-paste-{_unique()}.png"
    try:
        if system == "Windows":
            script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$img = [System.Windows.Forms.Clipboard]::GetImage(); "
                "if ($img -eq $null) { exit 1 } "
                f"$img.Save('{out}', [System.Drawing.Imaging.ImageFormat]::Png)"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, timeout=10,
            )
        elif system == "Darwin":
            proc = subprocess.run(
                ["osascript", "-e",
                 f'set theFile to (open for access POSIX file "{out}" with write permission)\n'
                 f'try\n'
                 f'  write (the clipboard as «class PNGf») to theFile\n'
                 f'end try\n'
                 f'close access theFile'],
                capture_output=True, timeout=10,
            )
        else:  # Linux — try Wayland then X11
            proc = subprocess.run(
                ["wl-paste", "-t", "image/png"], capture_output=True, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout:
                out.write_bytes(proc.stdout)
                return out
            proc = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                capture_output=True, timeout=10,
            )
            if proc.returncode == 0 and proc.stdout:
                out.write_bytes(proc.stdout)
                return out
            return None
    except Exception:
        return None

    if proc.returncode == 0 and out.is_file() and out.stat().st_size > 0:
        return out
    out.unlink(missing_ok=True)
    return None


_paste_counter = 0


def _unique() -> str:
    global _paste_counter
    _paste_counter += 1
    import time
    return f"{int(time.time())}-{_paste_counter}"
