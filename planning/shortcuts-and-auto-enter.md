# Shortcuts coverage, XDG portal integration, and auto-Enter-after-paste

Status: planning — 2026-04-27

Three related threads. Tackle in order; each phase is independently shippable.

---

## Phase 1 — Audit shortcut coverage and fill gaps

### Current state

Configured in `app/src/config.py:540-552`, registered in `app/src/main.py:1685-1729`:

| Action | Field | User's list |
|---|---|---|
| Toggle (start ↔ stop) | `hotkey_toggle` | covers "start", "start recording", "stop" |
| Tap toggle (cache) | `hotkey_tap_toggle` | — |
| Transcribe cached | `hotkey_transcribe` | "transcribe" |
| Clear | `hotkey_clear` | "delete" |
| Append | `hotkey_append` | "append" |
| Pause / Resume | `hotkey_pause` | "pause" |
| Retake | `hotkey_retake` | "retake" |
| Toggle window output | `hotkey_toggle_app` | — |
| Toggle clipboard | `hotkey_toggle_clipboard` | — |
| Toggle type-at-cursor | `hotkey_toggle_inject` | — |
| Toggle VAD | `hotkey_toggle_vad` | — |
| Toggle level meter | `hotkey_toggle_meter` | — |

### Gaps vs. requested list

- **"send transcribe"** — not present. Defined as: transcribe → paste → press Enter (uses Phase 3's auto-Enter pathway, but as a one-shot regardless of the global setting).
- **Separate "start" and "stop"** — not present (toggle handles both). Verdict: skip unless explicitly requested. Toggle is functionally equivalent and avoids two-binding overhead.

### Action items

- [ ] Add `hotkey_send_transcribe` field to `Config` (default `""`).
- [ ] Add combo + label in settings dialog (next to `hotkey_transcribe`).
- [ ] Register in `_setup_hotkeys()` → callback runs the existing transcribe path, then forces an Enter keystroke after paste regardless of `auto_press_enter_after_paste`.
- [ ] Wire into `_inject_text()` via an opt-in `force_enter` parameter so the global setting and the per-action override compose cleanly.

Out of scope: separate start-only / stop-only bindings.

---

## Phase 3 — Auto-press Enter after paste (do first, before Phase 2)

Small, self-contained, unblocks Phase 1's "send transcribe".

### Spec

- New config field: `auto_press_enter_after_paste: bool = False`.
- Surface in **main window** (not just settings dialog) — a checkbox near the output mode controls. Label: "Press Enter after paste (e.g. for chat apps)".
- Mirror in settings dialog for parity.
- In `_inject_text()` (`main.py:2217`), after the paste `ydotool key` call and before clipboard restore, if the setting is on (or `force_enter=True` is passed), send `ydotool key 28:1 28:0` (KEY_ENTER press + release).
- Small delay (~50ms) between paste and Enter so the paste lands first.

### Action items

- [ ] Add config field + persist.
- [ ] Add checkbox to main window layout.
- [ ] Add checkbox to settings dialog.
- [ ] Modify `_inject_text(text, force_enter=False)` signature.
- [ ] Test in Claude Code, Slack, Konsole, Kate (verify it doesn't fire a stray newline in plain editors when off).

### Edge cases

- Terminals: Enter executes the command — that's the intended behavior for chat-like usage. User opts in knowing this.
- Multi-line dictation: Enter still fires once at the end. Fine.

---

## Phase 2 — XDG GlobalShortcuts portal backend

Largest piece. Do **after** Phases 1 + 3 are merged so this lands as a pure infrastructure change.

### Why

Current backends (`evdev` + `pynput` in `app/src/hotkeys.py`) work but are hacks on Wayland — evdev needs input-group access; pynput needs XWayland. The correct primitive on Plasma 6 is `org.freedesktop.portal.GlobalShortcuts`. The user binds keys in **System Settings → Shortcuts** like any other app.

Note: there is no "XDG Global Shortcuts Manager" by that name — the relevant interface is the **GlobalShortcuts portal** (`org.freedesktop.portal.GlobalShortcuts`, available in xdg-desktop-portal 1.17+, supported by xdg-desktop-portal-kde on Plasma 6).

### Approach

- Add a third backend class to `hotkeys.py`: `PortalHotkeyListener`.
- On `start()`: open a portal session via D-Bus (`CreateSession`), call `BindShortcuts` with the registered action list, listen for `Activated` / `Deactivated` signals.
- Register order in `create_hotkey_listener()`: **portal first** (if available and session opens), then evdev, then pynput.
- Keep evdev/pynput as fallback — do **not** remove them. Some users (X11, non-KDE, headless test envs) still need them.
- Detection: portal is available if `org.freedesktop.portal.Desktop` is on the session bus *and* `org.freedesktop.portal.GlobalShortcuts` interface responds. Cache this check; don't re-probe per registration.

### Action items

- [ ] Add `dbus-next` (async) or `pydbus` to requirements. Prefer `dbus-next` for asyncio compat.
- [ ] Implement `PortalHotkeyListener` with the same `register/unregister/start/stop` interface as the existing listeners.
- [ ] Map our action names ("toggle", "transcribe", etc.) to portal shortcut IDs with human-readable descriptions (these show in System Settings).
- [ ] Suggested default keybindings sent to portal (user can override).
- [ ] Settings UI: when portal backend is active, replace the per-shortcut combo boxes with a single button "Configure shortcuts in System Settings…" that opens `systemsettings kcm_keys` — combos are meaningless when the portal owns the bindings.
- [ ] Migration: on first run with portal available, present a one-time dialog explaining the change and asking permission to register. Store choice in config.
- [ ] Document the change in README.

### Risks / open questions

- Portal `BindShortcuts` shows a system-modal consent dialog the first time — UX needs to set expectation.
- Some shortcuts (e.g. F-keys without modifiers) may be rejected by the portal/compositor; need to test what KDE actually accepts.
- Press + release semantics: portal emits separate `Activated`/`Deactivated` signals — maps cleanly to our existing `callback`/`release_callback` split.
- No portal support on KDE Plasma 5 or older xdg-desktop-portal-kde — fallback path must be solid.

---

## Execution order

1. **Phase 3** (auto-Enter setting) — quick win, ~30 min.
2. **Phase 1** (send-transcribe action) — depends on Phase 3's Enter pathway.
3. **Phase 2** (portal backend) — separate branch, larger surface, do last.
