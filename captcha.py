"""CAPTCHA detection and (optional) automated solving for the shared browser.

Layering (kept deliberately thin — see plan):
  * This module owns the MECHANICAL parts only: classifying which CAPTCHA is on
    the page, clicking checkbox widgets that live inside cross-origin iframes
    (which page-level browser_click cannot reach), cropping the challenge image,
    and asking a vision model to read distorted text.
  * POLICY (when to attempt, how many retries, when to give up to a human) lives
    in the `osint-captcha-solve` skill, not here. solve() just executes one
    deterministic attempt and reports a structured result.

Vision is delegated to the claude.exe harness (config.captcha_llm_cmd): it Reads
the cropped screenshot and answers. The vision model is chosen LOCAL-FIRST via
_harness_env() — a dedicated local VLM can be pinned independently of whatever
model the orchestrator itself runs, with an optional Anthropic fallback.

Ethical scope: authorized investigation only. This automates the tedium of
clearing a CAPTCHA on a site you are authorized to access; it is not an
authentication-bypass tool. Image-grid / picture-selection CAPTCHAs are NOT
auto-solved — they fall back to the human resume flow.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import anyio
from playwright.async_api import Page

from .config import TorConfig

# Kinds we classify a page into. checkbox kinds are solved mechanically; the
# image_text kind is solved with the vision harness; the rest need a human.
_CHECKBOX_KINDS = {"turnstile", "recaptcha_checkbox", "hcaptcha_checkbox"}
_HUMAN_KINDS = {"image_grid", "hcaptcha_image", "unknown"}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

_DETECT_JS = r"""
() => {
  const out = {present: false, kind: 'unknown',
              frame_selector: null, image_selector: null, input_selector: null};

  const hasFrame = (needle) =>
    Array.from(document.querySelectorAll('iframe'))
         .some(f => (f.src || '').includes(needle));

  // --- checkbox-style widgets (live in cross-origin iframes) ---
  if (hasFrame('challenges.cloudflare.com')) {
    out.present = true; out.kind = 'turnstile';
    out.frame_selector = 'iframe[src*="challenges.cloudflare.com"]';
    return out;
  }
  if (hasFrame('recaptcha/api2/bframe')) {     // image-grid challenge popup
    out.present = true; out.kind = 'image_grid';
    out.frame_selector = 'iframe[src*="recaptcha/api2/bframe"]';
    return out;
  }
  if (hasFrame('recaptcha/api2/anchor')) {     // the "I'm not a robot" checkbox
    out.present = true; out.kind = 'recaptcha_checkbox';
    out.frame_selector = 'iframe[src*="recaptcha/api2/anchor"]';
    return out;
  }
  if (hasFrame('hcaptcha.com')) {
    const checkbox = Array.from(document.querySelectorAll('iframe'))
      .find(f => (f.src || '').includes('hcaptcha') && (f.src || '').includes('checkbox'));
    out.present = true;
    out.kind = checkbox ? 'hcaptcha_checkbox' : 'hcaptcha_image';
    out.frame_selector = checkbox
      ? 'iframe[src*="hcaptcha"][src*="checkbox"]'
      : 'iframe[src*="hcaptcha"]';
    return out;
  }

  // --- classic image/text CAPTCHA (an <img> + a text input) ---
  const looksCaptcha = (s) => /captcha|seccode|verif|code/i.test(s || '');
  const sel = (el) => {
    if (!el) return null;
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.name) return el.tagName.toLowerCase() + '[name="' + CSS.escape(el.name) + '"]';
    return null;
  };
  let img = Array.from(document.querySelectorAll('img'))
    .find(i => looksCaptcha(i.src) || looksCaptcha(i.id) ||
               looksCaptcha(i.className) || looksCaptcha(i.alt));
  let input = Array.from(document.querySelectorAll('input'))
    .find(i => (i.type === 'text' || !i.type) &&
               (looksCaptcha(i.name) || looksCaptcha(i.id) ||
                looksCaptcha(i.placeholder) || looksCaptcha(i.className)));
  if (img && (input || /captcha/i.test(document.body.innerText))) {
    out.present = true; out.kind = 'image_text';
    out.image_selector = sel(img);
    out.input_selector = input ? sel(input) : null;
    return out;
  }
  return out;   // present:false
}
"""


async def detect(page: Page) -> dict[str, Any]:
    """Classify the CAPTCHA (if any) currently on the page.

    Returns {present, kind, frame_selector?, image_selector?, input_selector?}.
    """
    try:
        info = await page.evaluate(_DETECT_JS)
    except Exception as exc:  # noqa: BLE001
        return {"present": False, "kind": "unknown", "error": str(exc)}
    return info or {"present": False, "kind": "unknown"}


# ---------------------------------------------------------------------------
# Checkbox-style solving (iframe-aware — the part browser_click can't do)
# ---------------------------------------------------------------------------

# Per-kind candidate selectors for the clickable checkbox *inside* the iframe.
_CHECKBOX_TARGETS = {
    "recaptcha_checkbox": ["#recaptcha-anchor", ".recaptcha-checkbox", "#recaptcha-anchor-label"],
    "hcaptcha_checkbox": ["#checkbox", "#anchor", "div[role=checkbox]"],
    "turnstile": ["input[type=checkbox]", "label", "#challenge-stage"],
}


async def solve_checkbox(page: Page, info: dict[str, Any], config: TorConfig) -> dict[str, Any]:
    """Click a checkbox-style CAPTCHA inside its iframe and wait for it to pass.

    Returns {solved, kind, message}. A passed checkbox is best-effort verified
    (aria-checked / the widget disappearing). If clicking surfaces an image
    challenge instead, we report solved=False so the human flow takes over.
    """
    kind = info.get("kind", "unknown")
    frame_sel = info.get("frame_selector")
    if not frame_sel:
        return {"solved": False, "kind": kind, "message": "no frame selector"}

    frame = page.frame_locator(frame_sel)
    clicked = False
    for target in _CHECKBOX_TARGETS.get(kind, []):
        try:
            loc = frame.locator(target).first
            await loc.click(timeout=8_000)
            clicked = True
            break
        except Exception:  # noqa: BLE001
            continue
    if not clicked:
        return {"solved": False, "kind": kind, "message": "checkbox not clickable"}

    # Best-effort verification: poll for a passed state for up to the budget.
    deadline_ms = config.captcha_solve_timeout_ms
    waited = 0
    step = 1000
    while waited < deadline_ms:
        try:
            # reCAPTCHA / hCaptcha expose aria-checked on the anchor.
            checked = await frame.locator("[aria-checked=true]").count()
            if checked:
                return {"solved": True, "kind": kind, "message": "checkbox checked"}
        except Exception:  # noqa: BLE001
            pass
        # Turnstile: the widget removes its challenge frame once it passes.
        try:
            if await page.locator(frame_sel).count() == 0:
                return {"solved": True, "kind": kind, "message": "widget cleared"}
        except Exception:  # noqa: BLE001
            pass
        # An image-grid popup appearing means a human is needed.
        try:
            if await page.locator('iframe[src*="recaptcha/api2/bframe"]').count():
                return {"solved": False, "kind": "image_grid",
                        "message": "image challenge appeared", "reason": "needs_human"}
        except Exception:  # noqa: BLE001
            pass
        await anyio.sleep(step / 1000)
        waited += step

    return {"solved": False, "kind": kind, "message": "checkbox click did not pass in time"}


# ---------------------------------------------------------------------------
# Image/text solving via the claude.exe vision harness (local-first)
# ---------------------------------------------------------------------------

# The image is passed as a DIRECT user image block (base64) via
# --input-format stream-json — NOT via the Read tool. Read would wrap the image
# in a tool_result block, which local Anthropic-compat endpoints (LM Studio)
# reject ("Only text tool_result blocks are supported …"). A plain user image
# block is the normal VLM path and works both locally and on Anthropic.
_HARNESS_SYSTEM = (
    "You are an OCR engine for an AUTHORIZED OSINT investigation. You transcribe "
    "the characters shown in CAPTCHA images and output only those characters."
)
_HARNESS_TEXT = (
    "Read the CAPTCHA in this image. Output ONLY the exact characters shown — no "
    "explanation, no quotes, no code fences, no spaces. If you cannot read it, "
    "output exactly: UNKNOWN"
)


def _harness_env(config: TorConfig, *, anthropic: bool) -> dict[str, str]:
    """Build the environment for the vision harness subprocess.

    anthropic=False → LOCAL: apply the CAPTCHA-specific overrides on top of the
                      inherited env (Invoke-ClaudeLocal / .env).
    anthropic=True  → strip the local-routing vars so claude.exe talks to the
                      real Anthropic API (needs ANTHROPIC_API_KEY in the env).
    """
    env = dict(os.environ)
    if anthropic:
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"):
            env.pop(k, None)
        return env
    if config.captcha_base_url:
        env["ANTHROPIC_BASE_URL"] = config.captcha_base_url
    if config.captcha_model:
        env["ANTHROPIC_MODEL"] = config.captcha_model
    if config.captcha_auth_token:
        env["ANTHROPIC_AUTH_TOKEN"] = config.captcha_auth_token
    return env


def _build_stream_input(image_path: Path) -> str:
    """One stream-json user message: instruction text + a direct base64 image block."""
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    msg = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": _HARNESS_TEXT},
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b64}},
            ],
        },
    }
    return json.dumps(msg) + "\n"


def _parse_stream_result(stdout: str) -> str:
    """Extract the assistant's answer from --output-format stream-json events."""
    answer = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if obj.get("type") == "result" and not obj.get("is_error"):
            answer = obj.get("result", "") or answer
        elif obj.get("type") == "assistant" and not answer:
            for blk in obj.get("message", {}).get("content", []):
                if blk.get("type") == "text":
                    answer = blk.get("text", "") or answer
    return answer


def _run_harness(config: TorConfig, image_path: Path, *, anthropic: bool) -> str:
    """Blocking: ask the claude harness to read the image and return the answer.

    Feeds the image as a direct user image block (stream-json) and trims context
    (--strict-mcp-config, custom --system-prompt, neutral cwd) so a single solve
    doesn't drag in MCP servers / project CLAUDE.md / the full default prompt.
    """
    cmd = config.captcha_llm_cmd.split()
    # Local path: pin the VLM both ways (env ANTHROPIC_MODEL via _harness_env and
    # --model here — matching Invoke-ClaudeLocal's proven `claude --model ...`).
    if not anthropic and config.captcha_model and "--model" not in cmd and "-model" not in cmd:
        cmd += ["--model", config.captcha_model]
    cmd += [
        "--dangerously-skip-permissions", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",                       # required for stream-json output in -p
        "--strict-mcp-config",             # no MCP servers (none provided)
        "--system-prompt", _HARNESS_SYSTEM,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            cmd,
            input=_build_stream_input(image_path),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=tempfile.gettempdir(),     # neutral cwd → no project CLAUDE.md injection
            env=_harness_env(config, anthropic=anthropic),
            creationflags=creationflags,
            timeout=config.captcha_solve_timeout_ms / 1000,
        )
    except Exception as exc:  # noqa: BLE001  (timeout, spawn failure, …)
        print(f"[tor_mcp.captcha] harness failed (anthropic={anthropic}): {exc}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        print(f"[tor_mcp.captcha] harness rc={proc.returncode}: {proc.stderr.strip()[:300]}",
              file=sys.stderr)
        return ""
    return _clean_answer(_parse_stream_result(proc.stdout or ""))


def _clean_answer(raw: str) -> str:
    """Strip whitespace / quotes / code fences the model may wrap the answer in."""
    text = raw.strip()
    if not text:
        return ""
    # take the last non-empty line (claude may print reasoning before the answer)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        text = lines[-1]
    text = text.strip().strip("`").strip().strip('"').strip("'").strip()
    return "" if text.upper() == "UNKNOWN" else text


async def _ask_harness(image_path: Path, config: TorConfig) -> str:
    """Ask the vision harness to read the CAPTCHA image (local-first, then Anthropic)."""
    answer = await anyio.to_thread.run_sync(
        lambda: _run_harness(config, image_path, anthropic=False)
    )
    if answer:
        return answer
    if config.captcha_allow_anthropic_fallback:
        return await anyio.to_thread.run_sync(
            lambda: _run_harness(config, image_path, anthropic=True)
        )
    return ""


async def solve_image_text(page: Page, info: dict[str, Any], config: TorConfig) -> dict[str, Any]:
    """Crop the CAPTCHA image, read it with the vision harness, fill the input.

    Does NOT submit — the caller (skill) presses the submit/login button so the
    same flow that would follow a human-typed answer is reused.
    """
    image_sel = info.get("image_selector")
    input_sel = info.get("input_selector")
    if not image_sel:
        return {"solved": False, "kind": "image_text", "message": "no image selector",
                "reason": "needs_human"}

    config.screenshot_dir.mkdir(parents=True, exist_ok=True)
    name = datetime.now().strftime("captcha-%Y%m%d-%H%M%S-%f.png")
    path = config.screenshot_dir / name
    try:
        await page.locator(image_sel).first.screenshot(path=str(path))
    except Exception as exc:  # noqa: BLE001
        return {"solved": False, "kind": "image_text", "message": f"crop failed: {exc}",
                "reason": "needs_human"}

    answer = await _ask_harness(path, config)
    if not answer:
        return {"solved": False, "kind": "image_text", "image_path": str(path),
                "message": "vision harness returned no answer", "reason": "needs_human"}

    filled = False
    if input_sel:
        try:
            await page.fill(input_sel, answer, timeout=config.nav_timeout_ms)
            filled = True
        except Exception as exc:  # noqa: BLE001
            return {"solved": False, "kind": "image_text", "answer": answer,
                    "image_path": str(path), "message": f"fill failed: {exc}",
                    "reason": "needs_human"}

    return {
        "solved": filled,
        "kind": "image_text",
        "answer": answer,
        "image_path": str(path),
        "filled": filled,
        "input_selector": input_sel,
        "message": "filled answer; caller must submit" if filled
                   else "no input field found; answer returned for manual fill",
        **({} if filled else {"reason": "needs_human"}),
    }


# ---------------------------------------------------------------------------
# Top-level orchestration (one deterministic attempt)
# ---------------------------------------------------------------------------


async def solve(page: Page, config: Optional[TorConfig] = None) -> dict[str, Any]:
    """Run one CAPTCHA-solving attempt according to config.captcha_mode.

    Returns a structured result the caller interprets:
      {present, kind, mode, solved, ...}.
    Modes:
      "off"   → no-op ({attempted: False}).
      "human" → detect only; never auto-solve (existing human resume flow).
      "ai"    → checkbox kinds solved in-browser, image/text via the vision
                harness; image-grid / unknown report needs_human.
    """
    config = config or TorConfig.from_env()
    mode = (config.captcha_mode or "human").strip().lower()
    info = await detect(page)

    if mode == "off":
        return {"attempted": False, "mode": "off", "present": info.get("present", False),
                "kind": info.get("kind", "unknown")}

    if not info.get("present"):
        return {"present": False, "kind": info.get("kind", "unknown"),
                "mode": mode, "solved": False, "message": "no CAPTCHA detected"}

    kind = info.get("kind", "unknown")

    if mode == "human":
        return {"present": True, "kind": kind, "mode": "human", "solved": False,
                "message": "human resolution required (captcha_mode=human)"}

    # mode == "ai"
    if kind in _CHECKBOX_KINDS:
        result = await solve_checkbox(page, info, config)
    elif kind == "image_text":
        result = await solve_image_text(page, info, config)
    elif kind in _HUMAN_KINDS:
        result = {"solved": False, "kind": kind, "reason": "needs_human",
                  "message": f"{kind} cannot be auto-solved"}
    else:
        result = {"solved": False, "kind": kind, "reason": "needs_human",
                  "message": f"unhandled kind {kind!r}"}

    result.setdefault("present", True)
    result["mode"] = "ai"
    return result
