"""Opt-in Android acceptance using the real Expo app and isolated backend.

Requires an installed development client, Metro, adb reverse, and uiautomator2.
The backend, provider and runner isolation boundaries are documented in README.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests
import uiautomator2 as u2


def wait_for(predicate, description: str, timeout: float = 40) -> Any:
    """Wait for an observed state; never report a timed-out step as passed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise AssertionError(f"Timed out waiting for {description}")


def main() -> None:
    """Exercise native chat, composer, real gates, media and foreground restore."""
    manifest = json.loads(
        (Path(os.environ["MOBILE_E2E_RUN_DIR"]) / "manifest.json").read_text()
    )
    artifacts = Path(
        os.environ.get(
            "MOBILE_E2E_ARTIFACTS", "/workspace/.opencuria/playwright/mobile"
        )
    )
    artifacts.mkdir(parents=True, exist_ok=True)
    device = u2.connect(os.environ.get("ANDROID_SERIAL"))
    results: dict[str, Any] = {"runtime": "Android native development build"}
    base = manifest["http_url"].rstrip("/") + "/api/v1"
    login = requests.post(
        base + "/auth/login/",
        json={
            "email": manifest["email"],
            "password": os.environ["MOBILE_E2E_PASSWORD"],
        },
        timeout=10,
    )
    login.raise_for_status()
    headers = {
        "Authorization": "Bearer " + login.json()["access_token"],
        "X-Organization-Id": manifest["org_id"],
    }

    profile = requests.get(base + "/auth/me/", headers=headers, timeout=10)
    profile.raise_for_status()
    assert profile.json()["email"] == manifest["email"]

    def shot(name: str) -> None:
        device.screenshot(str(artifacts / f"android-{name}.png"))

    def hide_keyboard() -> None:
        state = device.shell("dumpsys input_method").output
        if "mInputShown=true" in state or "isInputViewShown=true" in state:
            device.press("back")

    def new_chat() -> None:
        wait_for(
            lambda: device(description="New chat", enabled=True).exists,
            "available workspace",
        )
        device(description="New chat").click()
        wait_for(
            lambda: device(description="Chat message", enabled=True).exists,
            "new chat composer",
        )
        time.sleep(0.5)

    def send(text: str) -> None:
        device(description="Chat message").set_text(text)
        hide_keyboard()
        wait_for(
            lambda: device(description="Send message", enabled=True).exists,
            "send control",
        )
        device(description="Send message").click()

    def drawer_search(title: str) -> None:
        device(description="Open conversation drawer").click()
        device(description="Search workspaces and conversations").set_text(title)
        item = device(descriptionStartsWith=title + " in ")
        wait_for(lambda: item.exists, title)
        item.click()
        wait_for(lambda: device(description="Chat message").exists, "conversation")

    try:
        # Reuse a saved login only when the actual app has restored its profile.
        device.app_start("org.opencuria.mobile.dev")
        wait_for(
            lambda: (
                device(description="Server URL").exists
                or device(description="Open conversation drawer").exists
            ),
            "app startup",
            90,
        )
        if device(description="Server URL").exists:
            device(description="Server URL").set_text(manifest["http_url"])
            device(description="Email").set_text(manifest["email"])
            device(description="Password").set_text(os.environ["MOBILE_E2E_PASSWORD"])
            hide_keyboard()
            device(description="Sign in").click()
        wait_for(
            lambda: device(description="Open conversation drawer").exists,
            "authenticated workspace",
        )
        results["authenticated_startup"] = True

        # File and skill suggestions are backed by real workspace/socket/catalogs.
        if device(description="Tools").exists:
            device(description="Tools").drag_to(0.02, 0.3, duration=0.5)
        new_chat()
        device(description="Chat message").set_text("@README")
        device(description="Chat message").click()
        wait_for(lambda: device(description="Add README.md").exists, "file mention")
        device(description="Add README.md").click()
        assert (
            "@file:/workspace/README.md"
            in device(description="Chat message").get_text()
        )
        results["file_mention"] = True
        device(description="Chat message").set_text("/")
        device(description="Chat message").click()
        wait_for(lambda: device(description="Add Mobile review").exists, "slash skill")
        device(description="Add Mobile review").click()
        wait_for(
            lambda: device(description="Remove Mobile review skill").exists,
            "selected skill chip",
        )
        results["slash_skill"] = True
        shot("composer")

        send("Show a mobile example. @file:/workspace/README.md")
        wait_for(
            lambda: device(textContains="Mobile fixture stream is live").exists,
            "streamed response",
            60,
        )
        wait_for(lambda: device(description="Send message").exists, "finished run")
        wait_for(
            lambda: device(description="Open image preview Fixture preview").exists,
            "authenticated workspace image",
        )
        wait_for(
            lambda: (
                device(description="Chat message").get_text() == "Message OpenCuria…"
            ),
            "cleared successful draft",
        )
        time.sleep(0.6)
        device.swipe(0.5, 0.72, 0.5, 0.48, duration=0.3)
        shot("chat")
        device(description="Open image preview Fixture preview").click()
        wait_for(
            lambda: device(description="Close image preview").exists, "image preview"
        )
        shot("image")
        device(description="Close image preview").click()
        results["stream_markdown_private_image"] = True

        # Always create fresh sessions: previous gate answers remain in history.
        new_chat()
        send("mobile-e2e:question")
        wait_for(
            lambda: device(text="Which color should the fixture choose?").exists,
            "question gate",
            60,
        )
        shot("question")
        device(descriptionStartsWith="Blue,").click()
        device(description="Submit answers").click()
        wait_for(
            lambda: device(textContains="selected color was recorded").exists,
            "question resume",
            60,
        )
        wait_for(lambda: device(description="Send message").exists, "question idle")
        results["question_gate"] = True

        new_chat()
        send("mobile-e2e:permission")
        wait_for(lambda: device(text="Permission required").exists, "permission", 60)
        shot("permission")
        device(description="Allow once").click()
        wait_for(
            lambda: device(textContains="local file edit completed").exists,
            "permission resume",
            60,
        )
        wait_for(lambda: device(description="Send message").exists, "permission idle")
        results["permission_gate"] = True

        new_chat()
        send("mobile-e2e:abort")
        wait_for(lambda: device(description="Stop OpenCuria").exists, "stop control")
        device(description="Stop OpenCuria").click()
        wait_for(lambda: device(description="Send message").exists, "aborted idle")
        results["abort_empty_draft"] = True

        # The current session survives a background/foreground transition.
        device.press("home")
        time.sleep(1)
        device.app_start("org.opencuria.mobile.dev")
        wait_for(
            lambda: device(description="Open conversation drawer").exists,
            "foreground restore",
        )
        results["foreground_reconnect"] = True

        drawer_search("Example chat — history")
        wait_for(lambda: device(description="Chat message").exists, "long history")
        results["existing_history"] = True

        # Native light/dark presentation, no render-only screenshots.
        device(description="Profile and settings").click()
        wait_for(lambda: device(description="Dark").exists, "appearance controls")
        device(description="Dark").click()
        device(description="Close").click()
        wait_for(lambda: not device(text="Profile & settings").exists, "closed profile")
        time.sleep(0.7)
        shot("dark-chat")
        device(description="Profile and settings").click()
        device(description="Light").click()
        device(description="Close").click()
        results["appearance"] = True

        results["passed"] = True
    except Exception:
        shot("failure")
        (artifacts / "android-failure.xml").write_text(device.dump_hierarchy())
        results["passed"] = False
        raise
    finally:
        (artifacts / "android-results.json").write_text(json.dumps(results, indent=2))
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
