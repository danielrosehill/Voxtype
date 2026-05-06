#!/usr/bin/env python3
"""Voxtype — Voice dictation with multimodal AI cleanup.

Single-window UI: record, transcribe, done. Format detection is automatic.
"""

import sys
import os
import time
import threading
import subprocess
from pathlib import Path
from typing import Optional

# Load .env
for env_path in [Path(__file__).parent / ".env", Path(__file__).parent.parent / ".env"]:
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip())
        break

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QComboBox, QLabel, QDialog, QFormLayout,
    QLineEdit, QCheckBox, QMessageBox, QFrame, QSizePolicy,
    QListWidget, QListWidgetItem, QSplitter, QSystemTrayIcon, QMenu,
    QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog,
    QProgressBar, QDoubleSpinBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QSize
from PyQt6.QtGui import QFont, QAction, QIcon, QPainter, QColor, QPen

from .config import (
    Config, load_config, save_config, build_cleanup_prompt,
    FORMAT_PRESETS, TONE_PRESETS, MODELS, DEFAULT_MODEL, DEFAULT_BUDGET_MODEL,
    REVIEW_MODEL, REVIEW_PROMPT, HOTKEY_OPTIONS, TRANSLATION_LANGUAGES,
    TTS_VOICE_OPTIONS,
    get_language_display_name, get_manufacturers, get_models_for_manufacturer,
    get_model_by_id, APP_VERSION,
)
from PyQt6.QtWidgets import QTabWidget
from .audio_recorder import AudioRecorder
from .audio_processor import prepare_audio_for_api, combine_wav_segments
from .transcription import (
    get_client, get_openrouter_key_info, get_openrouter_activity,
    TranscriptionError,
)
from .hotkeys import create_hotkey_listener
from .clipboard import copy_to_clipboard
from .vad_processor import is_vad_available
from .audio_feedback import get_feedback
from .tts_announcer import get_announcer, reset_announcer
from .history import TranscriptionHistory
from .recording_store import RecordingStore
from .recording_history_window import RecordingHistoryWindow
from .dictionary import (
    load_entries as load_dict_entries,
    save_entries as save_dict_entries,
    apply_substitutions,
    export_csv as dict_export_csv,
    export_json as dict_export_json,
    import_csv as dict_import_csv,
    import_json as dict_import_json,
)


class TranscriptionWorker(QThread):
    """Background thread for audio processing + transcription API call."""
    finished = pyqtSignal(str, float)  # text, elapsed_seconds
    error = pyqtSignal(str)
    status = pyqtSignal(str)  # status updates for UI
    def __init__(self, api_key, model, raw_audio_data, prompt,
                 review_enabled=False, vad_enabled=False,
                 mistral_api_key="", provider="openrouter"):
        super().__init__()
        self.api_key = api_key
        self.mistral_api_key = mistral_api_key
        self.model = model
        self.raw_audio_data = raw_audio_data
        self.prompt = prompt
        self.review_enabled = review_enabled
        self.vad_enabled = vad_enabled
        self.provider = provider

    def run(self):
        try:
            start = time.time()

            # Audio processing (VAD + AGC + MP3 compression) — runs off main thread
            self.status.emit("Processing audio...")
            processed, orig_dur, vad_dur = prepare_audio_for_api(
                self.raw_audio_data,
                vad_enabled=self.vad_enabled,
            )

            self.status.emit("Transcribing...")
            client = get_client(self.api_key, self.model,
                                mistral_api_key=self.mistral_api_key,
                                provider=self.provider)
            result = client.transcribe(processed, self.prompt)
            text = result.text

            # Second-pass review
            if self.review_enabled and text and len(text.strip()) > 20:
                try:
                    self.status.emit("Reviewing...")
                    review_client = get_client(self.api_key, REVIEW_MODEL)
                    review_result = review_client.review_text(text, REVIEW_PROMPT)
                    if review_result.text and review_result.text.strip():
                        text = review_result.text
                except Exception:
                    pass  # Review failure is non-fatal; use first-pass result

            # Custom dictionary substitutions — applied last, after any review
            try:
                text = apply_substitutions(text)
            except Exception:
                pass  # non-fatal

            elapsed = time.time() - start
            self.finished.emit(text, elapsed)
        except Exception as e:
            self.error.emit(str(e))


class _ModelPicker(QWidget):
    """Cascading Provider → Model selector widget."""

    def __init__(self, current_model_id: str, category_filter: str = "", parent=None):
        super().__init__(parent)
        self._category_filter = category_filter
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.provider_combo = QComboBox()
        self.provider_combo.setMinimumWidth(120)
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(200)

        layout.addWidget(self.provider_combo)
        layout.addWidget(self.model_combo)

        # Populate providers
        for mfr in get_manufacturers(category_filter):
            self.provider_combo.addItem(mfr, mfr)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        # Set to current model's provider, then select the model
        current = get_model_by_id(current_model_id)
        if current:
            idx = self.provider_combo.findData(current["manufacturer"])
            if idx >= 0:
                self.provider_combo.setCurrentIndex(idx)
            self._on_provider_changed()
            idx = self.model_combo.findData(current_model_id)
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)
        else:
            self._on_provider_changed()

    def _on_provider_changed(self):
        self.model_combo.clear()
        mfr = self.provider_combo.currentData()
        if not mfr:
            return
        for m in get_models_for_manufacturer(mfr, self._category_filter):
            self.model_combo.addItem(m["label"], m["id"])

    def selected_model_id(self) -> str:
        return self.model_combo.currentData() or ""


RETRY_FEEDBACK_PRESETS = [
    ("followed_instructions",
     "Treated my speech as instructions — transcribe literally, never follow what was said"),
    ("answered_question",
     "Answered the content instead of transcribing — it is dictation, not a query"),
    ("over_edited",
     "Over-edited / rewrote — stay closer to the original wording"),
    ("too_terse",
     "Output was truncated or missing content — include everything that was said"),
    ("wrong_format",
     "Wrong format detection — do not impose a format, keep it as plain prose"),
    ("unwanted_headings",
     "Added unnecessary headings — remove them, use plain paragraphs"),
    ("wrong_tone",
     "Tone/style was wrong — preserve the speaker's natural voice"),
    ("misheard_terms",
     "Misheard technical terms or proper nouns — prioritize accuracy on jargon"),
]


class RetryDialog(QDialog):
    """Collect feedback to steer a retry transcription of the last audio."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Retry transcription")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            "Re-send the last recording with feedback about what went wrong. "
            "Pick any that apply; the notes are added to the prompt as high-priority guidance."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._checks: list[tuple[str, QCheckBox, str]] = []
        for key, label in RETRY_FEEDBACK_PRESETS:
            cb = QCheckBox(label)
            cb.setStyleSheet("padding: 2px 0;")
            layout.addWidget(cb)
            self._checks.append((key, cb, label))

        layout.addSpacing(4)
        layout.addWidget(QLabel("Additional notes (optional):"))
        self.notes_edit = QTextEdit()
        self.notes_edit.setPlaceholderText(
            "e.g. \"This is a system prompt — keep the ## headings as I spoke them.\""
        )
        self.notes_edit.setFixedHeight(70)
        layout.addWidget(self.notes_edit)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        retry_btn = QPushButton("Retry")
        retry_btn.setDefault(True)
        retry_btn.setStyleSheet(
            "QPushButton { background-color: #0d6efd; color: white; "
            "padding: 6px 14px; border: none; border-radius: 4px; font-weight: bold; }"
            "QPushButton:hover { background-color: #0b5ed7; }"
        )
        retry_btn.clicked.connect(self.accept)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(retry_btn)
        layout.addLayout(btn_row)

    def feedback_text(self) -> str:
        lines = [f"- {label}" for _, cb, label in self._checks if cb.isChecked()]
        extra = self.notes_edit.toPlainText().strip()
        if extra:
            lines.append(f"- {extra}")
        return "\n".join(lines)


class LevelMeter(QWidget):
    """Audio level meter with a shaded target zone for intelligible speech.

    Level input is 0.0-1.0 (RMS/8000 from AudioRecorder). The target zone
    (~0.30-0.70) corresponds to the RMS range where dictation transcribes
    reliably — below is too quiet, above risks clipping.
    """

    TARGET_LO = 0.30
    TARGET_HI = 0.70

    def __init__(self, parent=None):
        super().__init__(parent)
        self._level = 0.0
        self.setFixedSize(QSize(160, 14))
        self.setToolTip(
            "Input level. Shaded band is the target range for clear dictation — "
            "aim to keep peaks inside it."
        )

    def set_level(self, level: float):
        level = max(0.0, min(1.0, level))
        if abs(level - self._level) < 0.005:
            return
        self._level = level
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w = self.width()
        h = self.height()

        p.fillRect(0, 0, w, h, QColor("#eeeeee"))

        lo_x = int(w * self.TARGET_LO)
        hi_x = int(w * self.TARGET_HI)
        p.fillRect(lo_x, 0, hi_x - lo_x, h, QColor(40, 167, 69, 40))

        fill_w = int(w * self._level)
        if fill_w > 0:
            if self._level < self.TARGET_LO:
                color = QColor("#f0ad4e")
            elif self._level > self.TARGET_HI:
                color = QColor("#dc3545")
            else:
                color = QColor("#28a745")
            p.fillRect(0, 0, fill_w, h, color)

        pen = QPen(QColor("#28a745"))
        pen.setWidth(1)
        p.setPen(pen)
        p.drawLine(lo_x, 0, lo_x, h)
        p.drawLine(hi_x, 0, hi_x, h)

        p.setPen(QPen(QColor("#cccccc")))
        p.drawRect(0, 0, w - 1, h - 1)
        p.end()


class SettingsDialog(QDialog):
    """Tabbed settings dialog."""

    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Settings")
        self.setMinimumWidth(500)

        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        outer.addWidget(tabs)

        # ── Tab 1: General ──
        general = QWidget()
        gl = QFormLayout(general)
        gl.setSpacing(12)

        # API Key
        self.api_key_edit = QLineEdit(config.openrouter_api_key)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-or-...")
        gl.addRow("OpenRouter API Key:", self.api_key_edit)

        self.mistral_key_edit = QLineEdit(config.mistral_api_key)
        self.mistral_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.mistral_key_edit.setPlaceholderText("Optional — routes Voxtral directly to Mistral")
        gl.addRow("Mistral API Key:", self.mistral_key_edit)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        gl.addRow(sep)

        # Default model (all models)
        self.default_picker = _ModelPicker(config.default_model)
        gl.addRow("Default model:", self.default_picker)

        # Budget model (budget only)
        self.budget_picker = _ModelPicker(config.default_budget_model, category_filter="Budget")
        gl.addRow("Budget model:", self.budget_picker)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        gl.addRow(sep2)

        # Personalization
        self.user_name_edit = QLineEdit(config.user_name)
        self.user_name_edit.setPlaceholderText("Your name (for email sign-offs)")
        gl.addRow("Name:", self.user_name_edit)

        self.email_edit = QLineEdit(config.email_address)
        self.email_edit.setPlaceholderText("your@email.com")
        gl.addRow("Email:", self.email_edit)

        self.signature_edit = QLineEdit(config.email_signature)
        gl.addRow("Sign-off:", self.signature_edit)

        self.user_signature_edit = QTextEdit()
        self.user_signature_edit.setPlainText(config.signature)
        self.user_signature_edit.setFixedHeight(72)
        self.user_signature_edit.setPlaceholderText(
            "Optional multi-line signature — appended to transcriptions "
            "when \"Append signature\" is enabled."
        )
        gl.addRow("Signature:", self.user_signature_edit)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
        gl.addRow(sep3)

        # Output modes
        self.out_app = QCheckBox("Show in app")
        self.out_app.setChecked(config.output_to_app)
        self.out_clipboard = QCheckBox("Copy to clipboard")
        self.out_clipboard.setChecked(config.output_to_clipboard)
        self.out_inject = QCheckBox("Type at cursor (ydotool)")
        self.out_inject.setChecked(config.output_to_inject)
        self.out_auto_enter = QCheckBox("Press Enter after paste (chat apps)")
        self.out_auto_enter.setChecked(config.auto_press_enter_after_paste)
        self.out_auto_enter.setToolTip(
            "After pasting at cursor, send an Enter keystroke. Useful for "
            "Claude Code, Slack, etc."
        )
        gl.addRow("Output:", self.out_app)
        gl.addRow("", self.out_clipboard)
        gl.addRow("", self.out_inject)
        gl.addRow("", self.out_auto_enter)

        tabs.addTab(general, "General")

        # ── Tab 2: Advanced ──
        advanced = QWidget()
        al = QFormLayout(advanced)
        al.setSpacing(12)

        # Features
        self.vad_check = QCheckBox("Voice Activity Detection (remove silence)")
        self.vad_check.setChecked(config.vad_enabled)
        if not is_vad_available():
            self.vad_check.setEnabled(False)
            self.vad_check.setToolTip("ten-vad not installed")
        al.addRow(self.vad_check)

        self.review_check = QCheckBox("Second-pass review (catches misheard words)")
        self.review_check.setChecked(config.review_enabled)
        al.addRow(self.review_check)

        self.show_meter_check = QCheckBox("Show audio level meter while recording")
        self.show_meter_check.setChecked(config.show_level_meter)
        self.show_meter_check.setToolTip(
            "Display an input-level meter with a target zone for intelligible speech."
        )
        al.addRow(self.show_meter_check)

        self.auto_stop_spin = QDoubleSpinBox()
        self.auto_stop_spin.setRange(0.0, 30.0)
        self.auto_stop_spin.setSingleStep(0.5)
        self.auto_stop_spin.setDecimals(1)
        self.auto_stop_spin.setSuffix(" s")
        self.auto_stop_spin.setSpecialValueText("Off")
        self.auto_stop_spin.setValue(config.auto_stop_silence_seconds)
        self.auto_stop_spin.setToolTip(
            "Automatically stop recording after this many seconds of silence "
            "(once speech has been detected). 0 disables."
        )
        al.addRow("Auto-stop on silence:", self.auto_stop_spin)

        sep4 = QFrame(); sep4.setFrameShape(QFrame.Shape.HLine)
        al.addRow(sep4)

        # Translation
        self.translation_combo = QComboBox()
        for code, name in TRANSLATION_LANGUAGES:
            self.translation_combo.addItem(name, code)
        idx = self.translation_combo.findData(config.translation_target)
        if idx >= 0:
            self.translation_combo.setCurrentIndex(idx)
        al.addRow("Translate to:", self.translation_combo)

        # Audio feedback
        self.feedback_combo = QComboBox()
        self.feedback_combo.addItem("Beeps", "beeps")
        self.feedback_combo.addItem("Voice (TTS)", "tts")
        self.feedback_combo.addItem("Silent", "silent")
        idx = self.feedback_combo.findData(config.audio_feedback_mode)
        if idx >= 0:
            self.feedback_combo.setCurrentIndex(idx)
        al.addRow("Audio feedback:", self.feedback_combo)

        # Default TTS voice pack
        self.voice_combo = QComboBox()
        for vid, vname in TTS_VOICE_OPTIONS:
            self.voice_combo.addItem(vname, vid)
        idx = self.voice_combo.findData(config.tts_voice)
        if idx >= 0:
            self.voice_combo.setCurrentIndex(idx)
        self.voice_combo.setToolTip(
            "Default TTS voice pack used for announcements. Can also be "
            "toggled on the main window."
        )
        al.addRow("TTS voice:", self.voice_combo)

        sep5 = QFrame(); sep5.setFrameShape(QFrame.Shape.HLine)
        al.addRow(sep5)

        # Hotkeys
        self.hotkey_toggle_combo = QComboBox()
        self.hotkey_tap_combo = QComboBox()
        self.hotkey_transcribe_combo = QComboBox()
        self.hotkey_send_transcribe_combo = QComboBox()
        self.hotkey_clear_combo = QComboBox()
        self.hotkey_append_combo = QComboBox()
        self.hotkey_pause_combo = QComboBox()
        self.hotkey_retake_combo = QComboBox()
        self.hotkey_toggle_app_combo = QComboBox()
        self.hotkey_toggle_clipboard_combo = QComboBox()
        self.hotkey_toggle_inject_combo = QComboBox()
        self.hotkey_toggle_vad_combo = QComboBox()
        self.hotkey_toggle_meter_combo = QComboBox()

        hotkey_combos = [
            (self.hotkey_toggle_combo, config.hotkey_toggle, "Toggle (start/stop):"),
            (self.hotkey_tap_combo, config.hotkey_tap_toggle, "Tap toggle (cache):"),
            (self.hotkey_transcribe_combo, config.hotkey_transcribe, "Transcribe cached:"),
            (self.hotkey_send_transcribe_combo, config.hotkey_send_transcribe, "Send transcribe (paste + Enter):"),
            (self.hotkey_clear_combo, config.hotkey_clear, "Clear:"),
            (self.hotkey_append_combo, config.hotkey_append, "Append:"),
            (self.hotkey_pause_combo, config.hotkey_pause, "Pause/Resume:"),
            (self.hotkey_retake_combo, config.hotkey_retake, "Retake:"),
            (self.hotkey_toggle_app_combo, config.hotkey_toggle_app, "Toggle window output:"),
            (self.hotkey_toggle_clipboard_combo, config.hotkey_toggle_clipboard, "Toggle clipboard:"),
            (self.hotkey_toggle_inject_combo, config.hotkey_toggle_inject, "Toggle type-at-cursor:"),
            (self.hotkey_toggle_vad_combo, config.hotkey_toggle_vad, "Toggle VAD:"),
            (self.hotkey_toggle_meter_combo, config.hotkey_toggle_meter, "Toggle level meter:"),
        ]
        for combo, current_value, label_text in hotkey_combos:
            for key_id, display_name in HOTKEY_OPTIONS:
                combo.addItem(display_name, key_id)
            idx = combo.findData(current_value)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            al.addRow(label_text, combo)

        tabs.addTab(advanced, "Advanced")

        # ── Tab 3: Dictionary ──
        dictionary_tab = QWidget()
        dl = QVBoxLayout(dictionary_tab)
        dl.setSpacing(8)

        info = QLabel(
            "Post-processing substitutions applied after transcription. "
            "Useful for names, jargon, or words the model consistently mishears. "
            "Prefer this over the system prompt for fixed corrections."
        )
        info.setWordWrap(True)
        dl.addWidget(info)

        self.dict_table = QTableWidget(0, 4)
        self.dict_table.setHorizontalHeaderLabels(
            ["Mistaken as", "Correct to", "Whole word", "Case sensitive"]
        )
        hdr = self.dict_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        for entry in load_dict_entries():
            self._append_dict_row(entry)
        dl.addWidget(self.dict_table)

        dict_btns = QHBoxLayout()
        add_btn = QPushButton("Add entry")
        add_btn.clicked.connect(lambda: self._append_dict_row(None))
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove_dict_row)
        import_btn = QPushButton("Import…")
        import_btn.clicked.connect(self._import_dictionary)
        export_btn = QPushButton("Export…")
        export_btn.clicked.connect(self._export_dictionary)
        dict_btns.addWidget(add_btn)
        dict_btns.addWidget(remove_btn)
        dict_btns.addStretch()
        dict_btns.addWidget(import_btn)
        dict_btns.addWidget(export_btn)
        dl.addLayout(dict_btns)

        tabs.addTab(dictionary_tab, "Dictionary")

        # ── Buttons ──
        btn_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(save_btn)
        outer.addLayout(btn_layout)

    def _append_dict_row(self, entry: dict | None):
        row = self.dict_table.rowCount()
        self.dict_table.insertRow(row)
        frm = (entry or {}).get("from", "")
        to = (entry or {}).get("to", "")
        whole = (entry or {}).get("whole_word", True)
        case_sens = (entry or {}).get("case_sensitive", False)

        self.dict_table.setItem(row, 0, QTableWidgetItem(frm))
        self.dict_table.setItem(row, 1, QTableWidgetItem(to))

        whole_cb = QCheckBox()
        whole_cb.setChecked(whole)
        case_cb = QCheckBox()
        case_cb.setChecked(case_sens)
        # Center the checkboxes in their cells
        for col, cb in ((2, whole_cb), (3, case_cb)):
            wrap = QWidget()
            lay = QHBoxLayout(wrap)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(cb)
            lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.dict_table.setCellWidget(row, col, wrap)

    def _remove_dict_row(self):
        rows = sorted({i.row() for i in self.dict_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.dict_table.removeRow(r)

    def _import_dictionary(self):
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Import dictionary", "",
            "Dictionary files (*.csv *.json);;CSV (*.csv);;JSON (*.json);;All files (*)"
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            if path.suffix.lower() == ".json":
                new_entries = dict_import_json(path)
            else:
                new_entries = dict_import_csv(path)
        except Exception as e:
            QMessageBox.warning(self, "Import failed", f"Could not read {path.name}:\n{e}")
            return

        if not new_entries:
            QMessageBox.information(self, "Import", "No entries found in file.")
            return

        # Ask: merge with existing or replace?
        existing = self._collect_dict_entries()
        if existing:
            choice = QMessageBox.question(
                self, "Import dictionary",
                f"Found {len(new_entries)} entries. Merge with your {len(existing)} "
                f"existing entries? (No = replace)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
            )
            if choice == QMessageBox.StandardButton.Cancel:
                return
            if choice == QMessageBox.StandardButton.Yes:
                # Merge: keep existing, add new entries whose `from` isn't already present
                seen = {e["from"].lower() for e in existing}
                merged = list(existing)
                for e in new_entries:
                    if e["from"].lower() not in seen:
                        merged.append(e)
                        seen.add(e["from"].lower())
                new_entries = merged

        self.dict_table.setRowCount(0)
        for entry in new_entries:
            self._append_dict_row(entry)

    def _export_dictionary(self):
        entries = self._collect_dict_entries()
        if not entries:
            QMessageBox.information(self, "Export", "Dictionary is empty.")
            return
        path_str, selected_filter = QFileDialog.getSaveFileName(
            self, "Export dictionary", "dictionary.csv",
            "CSV (*.csv);;JSON (*.json)"
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            if path.suffix.lower() == ".json" or "JSON" in selected_filter:
                if path.suffix.lower() != ".json":
                    path = path.with_suffix(".json")
                dict_export_json(entries, path)
            else:
                if path.suffix.lower() != ".csv":
                    path = path.with_suffix(".csv")
                dict_export_csv(entries, path)
        except Exception as e:
            QMessageBox.warning(self, "Export failed", str(e))

    def _collect_dict_entries(self) -> list[dict]:
        entries = []
        for row in range(self.dict_table.rowCount()):
            frm_item = self.dict_table.item(row, 0)
            to_item = self.dict_table.item(row, 1)
            frm = frm_item.text().strip() if frm_item else ""
            if not frm:
                continue
            to = to_item.text() if to_item else ""
            whole_wrap = self.dict_table.cellWidget(row, 2)
            case_wrap = self.dict_table.cellWidget(row, 3)
            whole_cb = whole_wrap.findChild(QCheckBox) if whole_wrap else None
            case_cb = case_wrap.findChild(QCheckBox) if case_wrap else None
            entries.append({
                "from": frm,
                "to": to,
                "whole_word": whole_cb.isChecked() if whole_cb else True,
                "case_sensitive": case_cb.isChecked() if case_cb else False,
            })
        return entries

    def get_config(self) -> Config:
        """Return updated config from dialog values."""
        save_dict_entries(self._collect_dict_entries())
        self.config.openrouter_api_key = self.api_key_edit.text().strip()
        self.config.mistral_api_key = self.mistral_key_edit.text().strip()
        self.config.default_model = self.default_picker.selected_model_id()
        self.config.default_budget_model = self.budget_picker.selected_model_id()
        self.config.user_name = self.user_name_edit.text().strip()
        self.config.email_address = self.email_edit.text().strip()
        self.config.email_signature = self.signature_edit.text().strip()
        self.config.signature = self.user_signature_edit.toPlainText().rstrip()
        self.config.vad_enabled = self.vad_check.isChecked()
        self.config.review_enabled = self.review_check.isChecked()
        self.config.show_level_meter = self.show_meter_check.isChecked()
        self.config.auto_stop_silence_seconds = float(self.auto_stop_spin.value())
        self.config.translation_target = self.translation_combo.currentData()
        self.config.output_to_app = self.out_app.isChecked()
        self.config.output_to_clipboard = self.out_clipboard.isChecked()
        self.config.output_to_inject = self.out_inject.isChecked()
        self.config.auto_press_enter_after_paste = self.out_auto_enter.isChecked()
        self.config.audio_feedback_mode = self.feedback_combo.currentData()
        self.config.tts_voice = self.voice_combo.currentData() or "ryan"
        self.config.hotkey_toggle = self.hotkey_toggle_combo.currentData()
        self.config.hotkey_tap_toggle = self.hotkey_tap_combo.currentData()
        self.config.hotkey_transcribe = self.hotkey_transcribe_combo.currentData()
        self.config.hotkey_send_transcribe = self.hotkey_send_transcribe_combo.currentData()
        self.config.hotkey_clear = self.hotkey_clear_combo.currentData()
        self.config.hotkey_append = self.hotkey_append_combo.currentData()
        self.config.hotkey_pause = self.hotkey_pause_combo.currentData()
        self.config.hotkey_retake = self.hotkey_retake_combo.currentData()
        self.config.hotkey_toggle_app = self.hotkey_toggle_app_combo.currentData()
        self.config.hotkey_toggle_clipboard = self.hotkey_toggle_clipboard_combo.currentData()
        self.config.hotkey_toggle_inject = self.hotkey_toggle_inject_combo.currentData()
        self.config.hotkey_toggle_vad = self.hotkey_toggle_vad_combo.currentData()
        self.config.hotkey_toggle_meter = self.hotkey_toggle_meter_combo.currentData()
        return self.config


class UsageDialog(QDialog):
    """Standalone Usage page — session stats + per-key OpenRouter spend.

    Spend is scoped to the active API key via /api/v1/key (not /credits, which
    is account-wide across all keys).
    """

    _cache: Optional[dict] = None  # {"key": {...}, "activity": [...]}
    _cache_at: float = 0.0
    _activity_error: str = ""

    def __init__(self, parent, api_key: str, session_stats: dict):
        super().__init__(parent)
        self.api_key = api_key
        self.setWindowTitle("Usage")
        self.setMinimumWidth(420)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(14)

        # ── Session stats ──
        s_header = QLabel("This session")
        s_header.setStyleSheet("font-size: 13px; font-weight: bold; color: #333;")
        outer.addWidget(s_header)

        secs = int(session_stats["record_seconds"])
        mins, rem = divmod(secs, 60)
        stats_grid = QFormLayout()
        stats_grid.setHorizontalSpacing(18)
        stats_grid.setVerticalSpacing(4)
        stats_grid.addRow("Date:", QLabel(session_stats["day"]))
        stats_grid.addRow("Transcriptions:", QLabel(str(session_stats["sessions"])))
        stats_grid.addRow("Words dictated:", QLabel(f"{session_stats['words']:,}"))
        stats_grid.addRow("Recording time:", QLabel(f"{mins}m {rem:02d}s"))
        stats_grid.addRow("Approx. WPM:", QLabel(str(session_stats["wpm"])))
        outer.addLayout(stats_grid)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(sep)

        # ── Key spend ──
        k_header = QLabel("OpenRouter — this API key")
        k_header.setStyleSheet("font-size: 13px; font-weight: bold; color: #333;")
        outer.addWidget(k_header)

        self.key_grid = QFormLayout()
        self.key_grid.setHorizontalSpacing(18)
        self.key_grid.setVerticalSpacing(4)
        outer.addLayout(self.key_grid)

        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet("color: #888; font-size: 11px;")
        outer.addWidget(self.meta_label)

        # ── Buttons ──
        btns = QHBoxLayout()
        btns.addStretch()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(lambda: self._load(force=True))
        btns.addWidget(self.refresh_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)
        outer.addLayout(btns)

        self._load(force=False)

    def _clear_key_grid(self):
        while self.key_grid.rowCount():
            self.key_grid.removeRow(0)

    def _load(self, force: bool):
        self._clear_key_grid()
        if not self.api_key:
            self.key_grid.addRow(QLabel("No API key set. Add one in Settings."))
            self.meta_label.setText("")
            return

        now = time.time()
        use_cache = (not force
                     and UsageDialog._cache is not None
                     and (now - UsageDialog._cache_at) < 60)
        if use_cache:
            bundle = UsageDialog._cache
        else:
            self.refresh_btn.setEnabled(False)
            self.refresh_btn.setText("Loading…")
            QApplication.processEvents()
            try:
                key_info = get_openrouter_key_info(self.api_key)
            except Exception as e:
                self.refresh_btn.setEnabled(True)
                self.refresh_btn.setText("Refresh")
                self.key_grid.addRow(QLabel(f"Error: {e}"))
                self.meta_label.setText("")
                return
            activity: list = []
            act_err = ""
            try:
                activity = get_openrouter_activity(self.api_key)
            except Exception as e:
                act_err = str(e)
            bundle = {"key": key_info, "activity": activity, "activity_error": act_err}
            UsageDialog._cache = bundle
            UsageDialog._cache_at = now
            UsageDialog._activity_error = act_err
            self.refresh_btn.setEnabled(True)
            self.refresh_btn.setText("Refresh")

        key_info = bundle.get("key") or {}
        activity = bundle.get("activity") or []
        act_err = bundle.get("activity_error") or ""

        # All-time usage comes from /api/v1/key
        all_time = float(key_info.get("usage") or 0.0)
        label = key_info.get("label") or "(unnamed key)"

        # Bucket activity into today / last 7d / last 30d
        today, week, month = self._bucket_activity(activity)

        def fmt(v: Optional[float]) -> QLabel:
            if v is None:
                lbl = QLabel("n/a")
                lbl.setStyleSheet("color: #888;")
                return lbl
            return QLabel(f"${v:,.2f}")

        self.key_grid.addRow("Key label:", QLabel(label))
        self.key_grid.addRow("Today:", fmt(today))
        self.key_grid.addRow("Last 7 days:", fmt(week))
        self.key_grid.addRow("Last 30 days:", fmt(month))
        self.key_grid.addRow("All time:", fmt(all_time))

        from datetime import datetime as _dt
        fetched = _dt.fromtimestamp(UsageDialog._cache_at).strftime("%H:%M:%S")
        src = "/api/v1/key + /api/v1/activity"
        meta = f"Fetched {fetched} · cached 60s · source: {src}"
        if act_err:
            meta += f"\nActivity unavailable ({act_err}) — Today/7d/30d shown as n/a."
        self.meta_label.setText(meta)

    @staticmethod
    def _bucket_activity(activity: list) -> tuple:
        """Return (today, 7d, 30d) totals in USD from an activity list.

        Returns None for a bucket if activity is empty (endpoint unavailable).
        """
        if not activity:
            return (None, None, None)
        from datetime import date, datetime as _dt, timedelta
        today = date.today()
        day_cutoff_7 = today - timedelta(days=6)   # inclusive 7-day window
        day_cutoff_30 = today - timedelta(days=29)
        sum_today = 0.0
        sum_7 = 0.0
        sum_30 = 0.0
        for entry in activity:
            if not isinstance(entry, dict):
                continue
            raw_date = entry.get("date") or entry.get("day") or entry.get("timestamp")
            if not raw_date:
                continue
            try:
                d = _dt.fromisoformat(str(raw_date).replace("Z", "+00:00")).date()
            except ValueError:
                try:
                    d = _dt.strptime(str(raw_date)[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
            usage = entry.get("usage")
            if usage is None:
                usage = entry.get("cost") or entry.get("total_cost") or 0.0
            try:
                usage = float(usage)
            except (TypeError, ValueError):
                continue
            if d == today:
                sum_today += usage
            if d >= day_cutoff_7:
                sum_7 += usage
            if d >= day_cutoff_30:
                sum_30 += usage
        return (sum_today, sum_7, sum_30)


class MainWindow(QMainWindow):
    """Main application window — clean single-page layout."""

    # Signals for thread-safe UI updates from hotkey callbacks
    _toggle_signal = pyqtSignal()
    _clear_signal = pyqtSignal()
    _tap_toggle_signal = pyqtSignal()
    _transcribe_signal = pyqtSignal()
    _send_transcribe_signal = pyqtSignal()
    _append_signal = pyqtSignal()
    _pause_signal = pyqtSignal()
    _retake_signal = pyqtSignal()
    _toggle_app_signal = pyqtSignal()
    _toggle_clipboard_signal = pyqtSignal()
    _toggle_inject_signal = pyqtSignal()
    _toggle_vad_signal = pyqtSignal()
    _toggle_meter_signal = pyqtSignal()
    # Audio level (float 0..1) from recorder thread → UI thread
    _level_signal = pyqtSignal(float)
    _silence_stop_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.recorder = AudioRecorder()
        self.recorder.on_error = self._on_recording_error
        self.hotkey_listener = None
        self.worker = None
        self._cached_segments: list[bytes] = []
        self._raw_text: str = ""  # Raw markdown text (for clipboard/append)
        self._append_mode: bool = False  # When True, transcription appends to existing text
        self._history = TranscriptionHistory(max_items=20)

        # Persistent on-disk recording store (24h retention + crash recovery)
        self._rec_store = RecordingStore()
        try:
            self._rec_store.cleanup_old()
        except Exception:
            pass
        self._active_entry_id: Optional[str] = None  # current in-flight transcription
        self._recovered_entry = None
        try:
            self._recovered_entry = self._rec_store.recover_crashed()
        except Exception:
            pass

        self._duration_timer = QTimer()
        self._duration_timer.timeout.connect(self._update_duration)

        # Undo/re-transcribe buffer: last raw audio + last inserted text
        self._last_raw_audio: Optional[bytes] = None
        self._last_inserted_text: str = ""
        self._text_before_last_insert: str = ""

        # One-shot flag: when True, the next transcription delivery pastes at
        # cursor and presses Enter regardless of the configured output modes.
        # Set by the send_transcribe hotkey, cleared after delivery.
        self._force_send_next: bool = False

        # Session stats (reset at local midnight)
        from datetime import date
        self._stats_day: date = date.today()
        self._stats_sessions: int = 0
        self._stats_words: int = 0
        self._stats_record_seconds: float = 0.0
        self._stats_speak_seconds: float = 0.0  # trimmed duration (post-VAD) used as WPM denom

        # OpenRouter credits cache
        self._credits_cache: Optional[dict] = None
        self._credits_cache_at: float = 0.0

        self._setup_ui()
        self._setup_tray()
        self._setup_hotkeys()
        self._connect_signals()

        # Check API key on startup
        if not self.config.openrouter_api_key:
            QTimer.singleShot(500, self._prompt_api_key)

        # Surface a banner if we recovered a partial recording from a crash
        if self._recovered_entry is not None:
            mins = int(self._recovered_entry.duration_seconds // 60)
            secs = int(self._recovered_entry.duration_seconds % 60)
            QTimer.singleShot(
                600,
                lambda: self._show_error_banner(
                    f"Recovered a partial recording ({mins}:{secs:02d}) from a "
                    f"previous session. Open Tools → Recording History (Ctrl+H) "
                    f"to re-transcribe it."
                ),
            )

    def _setup_ui(self):
        self.setWindowTitle("Voxtype")
        self.resize(self.config.window_width, self.config.window_height)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        # ── Top control bar ──
        controls = QHBoxLayout()
        controls.setSpacing(8)

        # Format selector
        format_label = QLabel("Format:")
        format_label.setStyleSheet("color: #888; font-size: 12px;")
        controls.addWidget(format_label)

        self.format_combo = QComboBox()
        self.format_combo.setMinimumWidth(140)
        last_category = None
        for key, data in FORMAT_PRESETS.items():
            cat = data.get("category", "")
            if cat != last_category and last_category is not None:
                self.format_combo.insertSeparator(self.format_combo.count())
            last_category = cat
            self.format_combo.addItem(data["label"], key)
        idx = self.format_combo.findData(self.config.format_preset)
        if idx >= 0:
            self.format_combo.setCurrentIndex(idx)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        controls.addWidget(self.format_combo)

        # Tone selector
        tone_label = QLabel("Tone:")
        tone_label.setStyleSheet("color: #888; font-size: 12px;")
        controls.addWidget(tone_label)

        self.tone_combo = QComboBox()
        self.tone_combo.setMinimumWidth(100)
        for key in TONE_PRESETS:
            self.tone_combo.addItem(key.capitalize(), key)
        idx = self.tone_combo.findData(self.config.tone)
        if idx >= 0:
            self.tone_combo.setCurrentIndex(idx)
        self.tone_combo.currentIndexChanged.connect(self._on_tone_changed)
        controls.addWidget(self.tone_combo)

        controls.addStretch()

        # Translation indicator (visible when translation is active)
        self.translation_label = QLabel("")
        self.translation_label.setStyleSheet(
            "color: #0d6efd; font-size: 11px; font-weight: bold; padding: 0 8px;"
        )
        controls.addWidget(self.translation_label)
        self._update_translation_indicator()

        # Provider selector (OpenRouter / Mistral)
        provider_label = QLabel("Provider:")
        provider_label.setStyleSheet("color: #888; font-size: 12px;")
        controls.addWidget(provider_label)

        self.provider_combo = QComboBox()
        self.provider_combo.setMinimumWidth(110)
        self.provider_combo.addItem("OpenRouter", "openrouter")
        self.provider_combo.addItem("Mistral", "mistral")
        idx = self.provider_combo.findData(self.config.provider)
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        controls.addWidget(self.provider_combo)

        # Model selector (Default / Budget / individual models)
        model_label = QLabel("Model:")
        model_label.setStyleSheet("color: #888; font-size: 12px;")
        controls.addWidget(model_label)

        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(200)
        self._populate_model_combo()
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        controls.addWidget(self.model_combo)

        layout.addLayout(controls)

        # ── Error banner (hidden unless an error occurs) ──
        self.error_banner = QLabel("")
        self.error_banner.setWordWrap(True)
        self.error_banner.setStyleSheet(
            "background-color: #f8d7da; color: #721c24; "
            "border: 1px solid #f5c6cb; border-radius: 4px; "
            "padding: 6px 10px; font-size: 12px;"
        )
        self.error_banner.setVisible(False)
        self.error_banner.mousePressEvent = lambda e: self._hide_error_banner()
        self.error_banner.setCursor(Qt.CursorShape.PointingHandCursor)
        self.error_banner.setToolTip("Click to dismiss")
        layout.addWidget(self.error_banner)

        # ── Main area: text editor + history sidebar ──
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Text editor (left, main)
        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText(
            "Press the record button or your hotkey to start dictating.\n\n"
            "Your transcription will appear here."
        )
        self.text_edit.setFont(QFont("Sans Serif", 12))
        self.text_edit.setAcceptRichText(False)
        self.text_edit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.text_edit.customContextMenuRequested.connect(self._on_text_context_menu)
        splitter.addWidget(self.text_edit)

        # History panel (right, collapsible accordion)
        self.history_widget = QWidget()
        history_layout = QVBoxLayout(self.history_widget)
        history_layout.setContentsMargins(0, 0, 0, 0)
        history_layout.setSpacing(4)

        history_header = QHBoxLayout()
        self.history_toggle_btn = QPushButton("▶ Recent")
        self.history_toggle_btn.setStyleSheet(
            "font-weight: bold; font-size: 12px; color: #888; "
            "border: none; text-align: left; padding: 2px 4px;"
        )
        self.history_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.history_toggle_btn.clicked.connect(self._toggle_history)
        history_header.addWidget(self.history_toggle_btn)
        history_header.addStretch()
        history_clear_btn = QPushButton("Clear")
        history_clear_btn.setFixedHeight(22)
        history_clear_btn.setStyleSheet("font-size: 10px; padding: 2px 8px;")
        history_clear_btn.clicked.connect(self._clear_history)
        history_header.addWidget(history_clear_btn)
        history_layout.addLayout(history_header)

        self.history_list = QListWidget()
        self.history_list.setMaximumWidth(250)
        self.history_list.setStyleSheet(
            "QListWidget { font-size: 11px; border: 1px solid #ddd; }"
            "QListWidget::item { padding: 4px 6px; }"
            "QListWidget::item:hover { background-color: #f0f0f0; }"
        )
        self.history_list.itemClicked.connect(self._on_history_item_clicked)
        self.history_list.setVisible(False)  # Hidden by default
        history_layout.addWidget(self.history_list)

        splitter.addWidget(self.history_widget)

        # Set initial sizes: text area gets all space, history header only
        splitter.setSizes([700, 0])
        splitter.setCollapsible(0, False)  # Text area can't be collapsed
        splitter.setCollapsible(1, True)   # History can be collapsed

        layout.addWidget(splitter)

        # ── Recording controls card (subtle shaded background) ──
        controls_card = QWidget()
        controls_card.setObjectName("ControlsCard")
        controls_card.setStyleSheet(
            "QWidget#ControlsCard { background-color: #f4f5f7; border-radius: 8px; }"
            "QWidget#ControlsCard QCheckBox { color: #495057; font-size: 11px; "
            "spacing: 5px; background: transparent; }"
            "QWidget#ControlsCard QLabel { background: transparent; }"
        )
        card_layout = QVBoxLayout(controls_card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(6)

        rec_bar = QHBoxLayout()
        rec_bar.setSpacing(6)

        # Record button (large, prominent)
        self.record_btn = QPushButton("\u25cf  Record")
        self.record_btn.setMinimumHeight(42)
        self.record_btn.setMinimumWidth(140)
        self.record_btn.setFont(QFont("Sans Serif", 13, QFont.Weight.Bold))
        self.record_btn.setStyleSheet(self._record_btn_style(False))
        self.record_btn.clicked.connect(self._toggle_recording)
        rec_bar.addWidget(self.record_btn)

        # Input level meter (visible while recording, only if enabled).
        # Lives in its own panel below the controls, not inline with them.
        self.level_meter = LevelMeter()
        self.level_meter.setVisible(False)

        # Pause button (enabled when recording)
        self.pause_btn = QPushButton("\u23f8  Pause")
        self.pause_btn.setMinimumHeight(36)
        self.pause_btn.setMinimumWidth(80)
        self.pause_btn.setStyleSheet(self._secondary_btn_style("#ffc107", "black", "#e0a800"))
        self.pause_btn.clicked.connect(self._pause_resume)
        self.pause_btn.setEnabled(False)
        rec_bar.addWidget(self.pause_btn)

        # Stop button (cache without transcribing)
        self.stop_btn = QPushButton("\u23f9  Stop")
        self.stop_btn.setMinimumHeight(36)
        self.stop_btn.setMinimumWidth(80)
        self.stop_btn.setStyleSheet(self._secondary_btn_style("#6c757d", "white", "#5a6268"))
        self.stop_btn.clicked.connect(self._stop_and_cache)
        self.stop_btn.setEnabled(False)
        rec_bar.addWidget(self.stop_btn)

        # Delete button (discard recording and go idle — no retake)
        self.delete_btn = QPushButton("\U0001f5d1  Delete")
        self.delete_btn.setMinimumHeight(36)
        self.delete_btn.setMinimumWidth(80)
        self.delete_btn.setStyleSheet(self._secondary_btn_style("#dc3545", "white", "#c82333"))
        self.delete_btn.setToolTip("Discard current recording")
        self.delete_btn.clicked.connect(self._delete_recording)
        self.delete_btn.setEnabled(False)
        rec_bar.addWidget(self.delete_btn)

        # Retake button (discard + restart)
        self.retake_btn = QPushButton("\u21bb  Retake")
        self.retake_btn.setMinimumHeight(36)
        self.retake_btn.setMinimumWidth(80)
        self.retake_btn.setStyleSheet(self._secondary_btn_style("#fd7e14", "white", "#e8690b"))
        self.retake_btn.clicked.connect(self._retake)
        self.retake_btn.setEnabled(False)
        rec_bar.addWidget(self.retake_btn)

        # Transcribe button (visible when cached segments exist)
        self.transcribe_btn = QPushButton("\u25b6  Transcribe")
        self.transcribe_btn.setMinimumHeight(36)
        self.transcribe_btn.setMinimumWidth(100)
        self.transcribe_btn.setStyleSheet(self._secondary_btn_style("#0d6efd", "white", "#0b5ed7"))
        self.transcribe_btn.clicked.connect(self._transcribe_cached)
        self.transcribe_btn.setVisible(False)
        rec_bar.addWidget(self.transcribe_btn)

        # Discard button (visible when cached segments exist)
        self.discard_btn = QPushButton("\U0001f5d1  Discard")
        self.discard_btn.setMinimumHeight(36)
        self.discard_btn.setMinimumWidth(80)
        self.discard_btn.setStyleSheet(self._secondary_btn_style("#dc3545", "white", "#c82333"))
        self.discard_btn.clicked.connect(self._discard_cached)
        self.discard_btn.setVisible(False)
        rec_bar.addWidget(self.discard_btn)

        # Append button (visible after transcription — record more and append)
        self.append_btn = QPushButton("\u2795  Append")
        self.append_btn.setMinimumHeight(36)
        self.append_btn.setMinimumWidth(90)
        self.append_btn.setStyleSheet(self._secondary_btn_style("#17a2b8", "white", "#138496"))
        self.append_btn.setToolTip("Record more and append to existing text")
        self.append_btn.clicked.connect(self._start_append_recording)
        self.append_btn.setVisible(False)
        rec_bar.addWidget(self.append_btn)

        # Segment indicator label
        self.segment_label = QLabel("")
        self.segment_label.setStyleSheet("color: #6c757d; font-weight: bold; font-size: 11px;")
        rec_bar.addWidget(self.segment_label)

        rec_bar.addStretch()

        card_layout.addLayout(rec_bar)

        # Divider between controls and audio options (very subtle)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setStyleSheet("color: #e4e6ea; background-color: #e4e6ea; max-height: 1px;")
        card_layout.addWidget(divider)

        # Compact audio-options strip — muted, inline, no separate box
        audio_opts_strip = QHBoxLayout()
        audio_opts_strip.setSpacing(14)
        audio_opts_strip.setContentsMargins(2, 0, 2, 0)

        self.vad_check_main = QCheckBox("VAD (trim silence)")
        self.vad_check_main.setChecked(self.config.vad_enabled)
        self.vad_check_main.setEnabled(is_vad_available())
        if not is_vad_available():
            self.vad_check_main.setToolTip("ten-vad not installed")
        else:
            self.vad_check_main.setToolTip(
                "Trim silent regions before sending to the model. "
                "Reduces cost and latency."
            )
        self.vad_check_main.toggled.connect(self._on_vad_toggled)
        audio_opts_strip.addWidget(self.vad_check_main)

        self.show_meter_check = QCheckBox("Show level meter")
        self.show_meter_check.setChecked(self.config.show_level_meter)
        self.show_meter_check.setToolTip(
            "Show audio level meter while recording (with target-zone indicator)."
        )
        self.show_meter_check.toggled.connect(self._on_show_meter_toggled)
        audio_opts_strip.addWidget(self.show_meter_check)

        audio_opts_strip.addWidget(self.level_meter)
        audio_opts_strip.addStretch()

        # Voice picker — quick-toggle the TTS voice pack used for announcements.
        # The default is set in Settings; this reflects/changes it on the fly.
        audio_opts_strip.addWidget(QLabel("Voice:"))
        self.voice_combo_main = QComboBox()
        for vid, vname in TTS_VOICE_OPTIONS:
            self.voice_combo_main.addItem(vname, vid)
        idx = self.voice_combo_main.findData(self.config.tts_voice)
        if idx >= 0:
            self.voice_combo_main.setCurrentIndex(idx)
        self.voice_combo_main.setToolTip(
            "TTS announcement voice pack. Change is instant; the Settings "
            "dialog has the same option for setting your default."
        )
        self.voice_combo_main.currentIndexChanged.connect(self._on_voice_changed)
        audio_opts_strip.addWidget(self.voice_combo_main)

        card_layout.addLayout(audio_opts_strip)

        layout.addWidget(controls_card)

        # ── Output controls bar ──
        output_bar = QHBoxLayout()
        output_bar.setSpacing(6)

        # Text-box output toggle (show transcription in the app window)
        self.app_check = QCheckBox("\U0001f4dd  Show in window")
        self.app_check.setChecked(self.config.output_to_app)
        self.app_check.setToolTip("Show transcription in the text box below")
        self.app_check.toggled.connect(self._on_app_toggled)
        output_bar.addWidget(self.app_check)

        # Clipboard toggle
        self.clipboard_check = QCheckBox("\U0001f4cb  Clipboard")
        self.clipboard_check.setChecked(self.config.output_to_clipboard)
        self.clipboard_check.setToolTip("Auto-copy transcription to clipboard")
        self.clipboard_check.toggled.connect(self._on_clipboard_toggled)
        output_bar.addWidget(self.clipboard_check)

        # Text injection toggle
        self.inject_check = QCheckBox("\u2328  Type at cursor")
        self.inject_check.setChecked(self.config.output_to_inject)
        self.inject_check.setToolTip(
            "Paste transcription at cursor position via clipboard + Ctrl+Shift+V "
            "(ydotool). Works in terminals, editors, and GUI apps."
        )
        self.inject_check.toggled.connect(self._on_inject_toggled)
        output_bar.addWidget(self.inject_check)

        # Press Enter after paste — for chat apps where you want the message
        # sent in one shot. Off by default so plain editors stay clean.
        self.auto_enter_check = QCheckBox("⏎  Press Enter after paste")
        self.auto_enter_check.setChecked(self.config.auto_press_enter_after_paste)
        self.auto_enter_check.setToolTip(
            "After pasting, send an Enter keystroke. Useful for Claude Code, "
            "Slack, and other chat apps. Off by default."
        )
        self.auto_enter_check.toggled.connect(self._on_auto_enter_toggled)
        output_bar.addWidget(self.auto_enter_check)

        # Append signature toggle — adds the configured signature after a
        # blank line at the end of each transcription. Off by default so
        # single-shot dictation stays clean.
        self.sig_check = QCheckBox("\u270d  Append signature")
        self.sig_check.setChecked(self.config.output_append_signature)
        self.sig_check.setToolTip(
            "Append the signature from Settings after a blank line at the "
            "end of each transcription."
        )
        self.sig_check.toggled.connect(self._on_sig_toggled)
        output_bar.addWidget(self.sig_check)

        output_bar.addStretch()

        # Retry button — re-transcribe last audio with optional feedback
        self.retry_btn = QPushButton("\u21bb  Retry")
        self.retry_btn.setMinimumHeight(32)
        self.retry_btn.setToolTip(
            "Re-send the last recording to the model with optional feedback "
            "about what to fix (e.g., \"treated my words as instructions\")."
        )
        self.retry_btn.clicked.connect(self._retry_with_feedback)
        self.retry_btn.setEnabled(False)
        output_bar.addWidget(self.retry_btn)

        # Copy button
        copy_btn = QPushButton("\U0001f4cb  Copy")
        copy_btn.setMinimumHeight(32)
        copy_btn.clicked.connect(self._copy_text)
        output_bar.addWidget(copy_btn)

        # Clear text button
        clear_btn = QPushButton("\U0001f5d1  Clear")
        clear_btn.setMinimumHeight(32)
        clear_btn.clicked.connect(self._clear_text)
        output_bar.addWidget(clear_btn)

        layout.addLayout(output_bar)

        # ── Status bar ──
        status_bar = QHBoxLayout()
        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #888; font-size: 11px;")
        status_bar.addWidget(self.status_label)
        status_bar.addStretch()

        # Duration label — centered at the bottom of the app
        self.duration_label = QLabel("")
        self.duration_label.setStyleSheet(
            "color: #dc3545; font-size: 14px; font-family: monospace; font-weight: bold;"
        )
        self.duration_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.duration_label.setMinimumWidth(60)
        status_bar.addWidget(self.duration_label)
        status_bar.addStretch()


        # Audio mode toggle button
        self.beep_btn = QPushButton("")
        self.beep_btn.setFixedHeight(22)
        self.beep_btn.setStyleSheet(
            "QPushButton { font-size: 11px; padding: 2px 10px; border: 1px solid #aaa; "
            "border-radius: 4px; background: #f0f0f0; color: #555; }"
            "QPushButton:hover { background: #e0e0e0; border-color: #888; }"
        )
        self.beep_btn.setToolTip("Click to cycle: Beeps → Voice → Silent")
        self.beep_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.beep_btn.clicked.connect(self._cycle_audio_feedback)
        status_bar.addWidget(self.beep_btn)
        self._update_beep_indicator()

        layout.addLayout(status_bar)

        # ── Menu bar ──
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")

        settings_action = QAction("&Settings...", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._open_settings)
        file_menu.addAction(settings_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        edit_menu = menu.addMenu("&Edit")
        self.undo_insert_action = QAction("&Undo last insert", self)
        self.undo_insert_action.setShortcut("Ctrl+Z")
        self.undo_insert_action.triggered.connect(self._undo_last_insert)
        self.undo_insert_action.setEnabled(False)
        edit_menu.addAction(self.undo_insert_action)

        self.retranscribe_action = QAction("&Re-transcribe last audio…", self)
        self.retranscribe_action.setShortcut("Ctrl+R")
        self.retranscribe_action.triggered.connect(self._retranscribe_last)
        self.retranscribe_action.setEnabled(False)
        edit_menu.addAction(self.retranscribe_action)

        tools_menu = menu.addMenu("&Tools")
        usage_action = QAction("&Usage…", self)
        usage_action.setShortcut("Ctrl+U")
        usage_action.triggered.connect(self._show_usage)
        tools_menu.addAction(usage_action)

        recording_history_action = QAction("Recording &History…", self)
        recording_history_action.setShortcut("Ctrl+H")
        recording_history_action.triggered.connect(self._show_recording_history)
        tools_menu.addAction(recording_history_action)

        help_menu = menu.addMenu("&Help")
        models_action = QAction("&Supported Models...", self)
        models_action.triggered.connect(self._show_models_info)
        help_menu.addAction(models_action)
        help_menu.addSeparator()
        about_action = QAction("&About...", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _record_btn_style(self, recording: bool) -> str:
        if recording:
            return """
                QPushButton {
                    background-color: #dc3545;
                    color: white;
                    border: none;
                    border-radius: 6px;
                    padding: 8px 16px;
                }
                QPushButton:hover { background-color: #c82333; }
            """
        return """
            QPushButton {
                background-color: #28a745;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
            }
            QPushButton:hover { background-color: #218838; }
        """

    def _secondary_btn_style(self, bg: str, fg: str, hover: str) -> str:
        return f"""
            QPushButton {{
                background-color: {bg};
                color: {fg};
                border: none;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: bold;
            }}
            QPushButton:hover {{ background-color: {hover}; }}
            QPushButton:disabled {{ background-color: #ccc; color: #888; }}
        """

    def _effective_model(self) -> str:
        """Return the model ID that will actually be used for transcription."""
        if self.config.active_model:
            return self.config.active_model
        return self.config.default_model

    def _model_display_name(self, model_id: str = "") -> str:
        model_id = model_id or self._effective_model()
        m = get_model_by_id(model_id)
        return m["label"] if m else model_id

    def _short_model_name(self, model_id: str) -> str:
        """Model name without the vendor in parentheses, e.g. 'Gemini 3.1 Flash Lite'."""
        m = get_model_by_id(model_id)
        if not m:
            return model_id
        label = m["label"]
        # Strip trailing " (Vendor)" suffix
        if "(" in label:
            label = label[:label.rfind("(")].strip()
        return label

    def _models_for_provider(self) -> list[dict]:
        """Return the model list filtered to the current provider."""
        if self.config.provider == "mistral":
            return [m for m in MODELS if m["manufacturer"] == "Mistral"]
        return list(MODELS)

    def _populate_model_combo(self):
        """Fill the main UI model combo with Default, Budget, then all models."""
        self.model_combo.blockSignals(True)
        self.model_combo.clear()

        available = self._models_for_provider()
        available_ids = {m["id"] for m in available}

        if self.config.provider == "openrouter":
            default_name = self._short_model_name(self.config.default_model)
            budget_name = self._short_model_name(self.config.default_budget_model)
            self.model_combo.addItem("── Defaults ──")
            self.model_combo.model().item(self.model_combo.count() - 1).setEnabled(False)
            self.model_combo.addItem(f"  {default_name}", "__default__")
            self.model_combo.addItem(f"  Budget: {budget_name}", "__budget__")
            self.model_combo.insertSeparator(self.model_combo.count())

        for cat in ["Standard", "Budget"]:
            models_in_cat = [m for m in available if m["category"] == cat]
            if not models_in_cat:
                continue
            self.model_combo.addItem(f"── {cat} ──")
            self.model_combo.model().item(self.model_combo.count() - 1).setEnabled(False)
            for model in models_in_cat:
                short = self._short_model_name(model["id"])
                self.model_combo.addItem(f"  {short}", model["id"])
            self.model_combo.insertSeparator(self.model_combo.count())

        # Select current
        active = self.config.active_model
        default_idx = self.model_combo.findData("__default__")
        budget_idx = self.model_combo.findData("__budget__")
        if self.config.provider == "openrouter" and (not active or active == self.config.default_model):
            self.model_combo.setCurrentIndex(default_idx)
        elif self.config.provider == "openrouter" and active == self.config.default_budget_model:
            self.model_combo.setCurrentIndex(budget_idx)
        elif active and active in available_ids:
            idx = self.model_combo.findData(active)
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)
        else:
            # Fall back to first real model in the provider list
            for i in range(self.model_combo.count()):
                data = self.model_combo.itemData(i)
                if data and data not in ("__default__", "__budget__"):
                    self.model_combo.setCurrentIndex(i)
                    self.config.active_model = data
                    break

        self.model_combo.blockSignals(False)

    def _on_provider_changed(self):
        self.config.provider = self.provider_combo.currentData() or "openrouter"
        save_config(self.config)
        self._populate_model_combo()

    def _on_model_changed(self):
        data = self.model_combo.currentData()
        if data == "__default__":
            self.config.active_model = ""
        elif data == "__budget__":
            self.config.active_model = self.config.default_budget_model
        elif data:
            self.config.active_model = data

    def _setup_tray(self):
        """Set up system tray icon with context menu."""
        self.tray = QSystemTrayIcon(self)
        self._tray_state = "idle"

        # Icons from theme (fallback to standard pixmaps)
        app_icon_path = os.path.join(os.path.dirname(__file__), "..", "assets", "icon.png")
        if os.path.exists(app_icon_path):
            self._tray_icon_idle = QIcon(app_icon_path)
        else:
            self._tray_icon_idle = QIcon.fromTheme("audio-input-microphone")
        self._tray_icon_recording = QIcon.fromTheme(
            "media-record", self.style().standardIcon(self.style().StandardPixmap.SP_DialogNoButton))
        self._tray_icon_transcribing = QIcon.fromTheme(
            "emblem-synchronizing", self.style().standardIcon(self.style().StandardPixmap.SP_BrowserReload))
        self._tray_icon_complete = QIcon.fromTheme(
            "emblem-ok", self.style().standardIcon(self.style().StandardPixmap.SP_DialogApplyButton))

        self.tray.setIcon(self._tray_icon_idle)
        self.setWindowIcon(self._tray_icon_idle)

        # Context menu
        tray_menu = QMenu()
        tray_menu.addAction("Show/Hide", self._tray_toggle_window)
        tray_menu.addSeparator()
        self._tray_record_action = tray_menu.addAction("Record", self._toggle_recording)
        self._tray_transcribe_action = tray_menu.addAction("Transcribe Cached", self._transcribe_cached)
        self._tray_transcribe_action.setEnabled(False)
        tray_menu.addSeparator()
        tray_menu.addAction("Settings...", self._open_settings)
        tray_menu.addAction("Quit", self.close)
        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.setToolTip("Voxtype — Ready")
        self.tray.show()

    def _update_tray_state(self, state: str):
        """Update tray icon and tooltip based on app state."""
        self._tray_state = state
        if state == "idle":
            self.tray.setIcon(self._tray_icon_idle)
            self.tray.setToolTip("Voxtype — Ready")
            self._tray_record_action.setText("Record")
        elif state == "recording":
            self.tray.setIcon(self._tray_icon_recording)
            self.tray.setToolTip("Voxtype — Recording...")
            self._tray_record_action.setText("Stop + Transcribe")
        elif state == "transcribing":
            self.tray.setIcon(self._tray_icon_transcribing)
            self.tray.setToolTip("Voxtype — Transcribing...")
        elif state == "complete":
            self.tray.setIcon(self._tray_icon_complete)
            self.tray.setToolTip("Voxtype — Done")
            # Revert to idle after 3 seconds
            QTimer.singleShot(3000, lambda: self._update_tray_state("idle")
                              if self._tray_state == "complete" else None)
        elif state == "cached":
            self.tray.setIcon(self._tray_icon_idle)
            n = len(self._cached_segments)
            self.tray.setToolTip(f"Voxtype — {n} segment{'s' if n != 1 else ''} cached")
            self._tray_record_action.setText("Record")
        self._tray_transcribe_action.setEnabled(bool(self._cached_segments))

    def _tray_toggle_window(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()
            self.activateWindow()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._tray_toggle_window()

    def _update_translation_indicator(self):
        """Update the translation indicator label."""
        if self.config.translation_target:
            name = get_language_display_name(self.config.translation_target)
            self.translation_label.setText(f"\u2192 {name}")
            self.translation_label.setToolTip(f"Translation mode: output will be in {name}")
            self.translation_label.show()
        else:
            self.translation_label.setText("")
            self.translation_label.hide()

    def _update_segment_indicator(self):
        """Update segment count and transcribe/discard button visibility."""
        n = len(self._cached_segments)
        has_cached = n > 0
        if has_cached:
            self.segment_label.setText(f"{n} seg{'s' if n > 1 else ''}")
        else:
            self.segment_label.setText("")
        self.transcribe_btn.setVisible(has_cached)
        self.discard_btn.setVisible(has_cached)

    def _discard_cached(self):
        """Discard all cached audio segments."""
        self._cached_segments = []
        self._update_segment_indicator()
        self._audio_feedback("play_clear", "announce_discarded")
        self.duration_label.setText("")
        self.status_label.setText("Discarded")
        self._update_tray_state("idle")

    def _connect_signals(self):
        self._toggle_signal.connect(self._toggle_recording)
        self._clear_signal.connect(self._clear_recording)
        self._tap_toggle_signal.connect(self._tap_toggle)
        self._transcribe_signal.connect(self._transcribe_cached)
        self._send_transcribe_signal.connect(self._send_transcribe)
        self._append_signal.connect(self._start_append)
        self._pause_signal.connect(self._pause_resume)
        self._retake_signal.connect(self._retake)
        self._toggle_app_signal.connect(self._toggle_app_mode)
        self._toggle_clipboard_signal.connect(self._toggle_clipboard_mode)
        self._toggle_inject_signal.connect(self._toggle_inject_mode)
        self._toggle_vad_signal.connect(self._toggle_vad_mode)
        self._toggle_meter_signal.connect(self._toggle_meter_mode)
        self._level_signal.connect(self._on_level)
        self._silence_stop_signal.connect(self._on_silence_auto_stop)

        # Wire recorder callbacks (thread-safe via signals)
        self.recorder.on_level = lambda lvl: self._level_signal.emit(lvl)
        self.recorder.on_silence_timeout = lambda: self._silence_stop_signal.emit()

    def _setup_hotkeys(self):
        if self.hotkey_listener:
            self.hotkey_listener.stop()

        self.hotkey_listener = create_hotkey_listener()

        hk = self.config
        if hk.hotkey_toggle:
            self.hotkey_listener.register("toggle", hk.hotkey_toggle,
                                          lambda: self._toggle_signal.emit())
        if hk.hotkey_tap_toggle:
            self.hotkey_listener.register("tap_toggle", hk.hotkey_tap_toggle,
                                          lambda: self._tap_toggle_signal.emit())
        if hk.hotkey_transcribe:
            self.hotkey_listener.register("transcribe", hk.hotkey_transcribe,
                                          lambda: self._transcribe_signal.emit())
        if hk.hotkey_send_transcribe:
            self.hotkey_listener.register("send_transcribe", hk.hotkey_send_transcribe,
                                          lambda: self._send_transcribe_signal.emit())
        if hk.hotkey_clear:
            self.hotkey_listener.register("clear", hk.hotkey_clear,
                                          lambda: self._clear_signal.emit())
        if hk.hotkey_append:
            self.hotkey_listener.register("append", hk.hotkey_append,
                                          lambda: self._append_signal.emit())
        if hk.hotkey_pause:
            self.hotkey_listener.register("pause", hk.hotkey_pause,
                                          lambda: self._pause_signal.emit())
        if hk.hotkey_retake:
            self.hotkey_listener.register("retake", hk.hotkey_retake,
                                          lambda: self._retake_signal.emit())
        if hk.hotkey_toggle_app:
            self.hotkey_listener.register("toggle_app", hk.hotkey_toggle_app,
                                          lambda: self._toggle_app_signal.emit())
        if hk.hotkey_toggle_clipboard:
            self.hotkey_listener.register("toggle_clipboard", hk.hotkey_toggle_clipboard,
                                          lambda: self._toggle_clipboard_signal.emit())
        if hk.hotkey_toggle_inject:
            self.hotkey_listener.register("toggle_inject", hk.hotkey_toggle_inject,
                                          lambda: self._toggle_inject_signal.emit())
        if hk.hotkey_toggle_vad:
            self.hotkey_listener.register("toggle_vad", hk.hotkey_toggle_vad,
                                          lambda: self._toggle_vad_signal.emit())
        if hk.hotkey_toggle_meter:
            self.hotkey_listener.register("toggle_meter", hk.hotkey_toggle_meter,
                                          lambda: self._toggle_meter_signal.emit())

        self.hotkey_listener.start()

    # ── Audio feedback ──

    def _play_beep(self, beep_method: str):
        """Play a beep sound if in beeps mode."""
        if self.config.audio_feedback_mode == "beeps":
            getattr(get_feedback(), beep_method)()

    def _play_tts(self, announce_method: str):
        """Play a TTS announcement if in tts mode."""
        if self.config.audio_feedback_mode == "tts":
            getattr(get_announcer(self.config.tts_voice), announce_method)()

    def _audio_feedback(self, beep_method: str, tts_method: str, *tts_args, **tts_kwargs):
        """Play audio feedback based on current mode. Extra positional/keyword
        args are forwarded to the TTS method (no-op for beep mode)."""
        mode = self.config.audio_feedback_mode
        if mode == "beeps":
            getattr(get_feedback(), beep_method)()
        elif mode == "tts":
            getattr(get_announcer(self.config.tts_voice), tts_method)(*tts_args, **tts_kwargs)

    # ── Recording controls ──

    def _toggle_recording(self):
        """Toggle: if recording, stop + transcribe. If idle, start recording."""
        if self.recorder.is_recording:
            self._stop_and_transcribe()
        else:
            self._append_mode = False
            self._start_recording()

    def _start_recording(self):
        if self.recorder.is_recording:
            return
        # Play feedback BEFORE starting recording (so mic doesn't capture it)
        self._audio_feedback("play_start", "announce_recording")
        QTimer.singleShot(200, self._begin_recording)

    def _begin_recording(self):
        if self.recorder.is_recording:
            return
        # Apply auto-stop-on-silence config (0 disables)
        self.recorder.silence_timeout_seconds = float(
            getattr(self.config, "auto_stop_silence_seconds", 0.0) or 0.0
        )
        # Wire PCM spill for crash recovery. Writes raw int16 frames to a
        # known path alongside the in-memory buffer; cleared on clean stop.
        try:
            self.recorder.spill_path = str(self._rec_store.active_pcm_path())
        except Exception:
            self.recorder.spill_path = None
        if self.recorder.start_recording():
            try:
                self._rec_store.mark_active(self.recorder.actual_sample_rate)
            except Exception:
                pass
            self.level_meter.set_level(0.0)
            self.level_meter.setVisible(bool(self.config.show_level_meter))
            if not self._append_mode:
                self.text_edit.clear()
                self._raw_text = ""
            self.append_btn.setVisible(False)
            self.record_btn.setText("\u23f9  Transcribe")
            self.record_btn.setStyleSheet(self._record_btn_style(True))
            self.pause_btn.setEnabled(True)
            self.pause_btn.setText("\u23f8  Pause")
            self.stop_btn.setEnabled(True)
            self.delete_btn.setEnabled(True)
            self.retake_btn.setEnabled(True)
            self.transcribe_btn.setVisible(False)
            self.status_label.setText("Recording...")
            self.duration_label.setText("0:00")
            self._duration_timer.start(500)
            self._update_tray_state("recording")

    def _stop_and_transcribe(self):
        if not self.recorder.is_recording:
            return
        self._duration_timer.stop()
        duration_s = self.recorder.get_duration()
        audio_data = self.recorder.stop_recording()
        self._rec_store.clear_active()
        # "Audio sent." for short recordings; the longer
        # "Audio sent. Waiting for transcription." plays once the recording
        # is long enough that the user is likely to wait noticeably.
        is_long = duration_s >= self.config.tts_long_recording_threshold_s
        self._audio_feedback("play_stop", "announce_audio_sent", waiting=is_long)
        self._reset_record_buttons()

        # If we have cached segments, combine them with this recording
        if self._cached_segments:
            self._cached_segments.append(audio_data)
            audio_data = combine_wav_segments(self._cached_segments)
            self._cached_segments = []
            self._update_segment_indicator()

        self._transcribe(audio_data)

    def _stop_and_cache(self):
        """Stop recording and cache audio without transcribing."""
        if not self.recorder.is_recording:
            return
        self._duration_timer.stop()
        audio_data = self.recorder.stop_recording()
        self._rec_store.clear_active()
        self._cached_segments.append(audio_data)
        self._audio_feedback("play_cached", "announce_cached")
        self._reset_record_buttons()
        self._update_segment_indicator()
        n = len(self._cached_segments)
        self.status_label.setText(f"Stopped — {n} segment{'s' if n > 1 else ''} cached")
        self._update_tray_state("cached")

    def _reset_record_buttons(self):
        """Reset recording buttons to idle state."""
        self.record_btn.setText("\u25cf  Record")
        self.record_btn.setStyleSheet(self._record_btn_style(False))
        self.pause_btn.setText("\u23f8  Pause")
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.delete_btn.setEnabled(False)
        self.retake_btn.setEnabled(False)
        self.level_meter.setVisible(False)
        self.level_meter.set_level(0.0)

    def _on_level(self, level: float):
        """Update the level meter (called on UI thread via signal)."""
        if self.level_meter.isVisible():
            self.level_meter.set_level(level)

    def _on_show_meter_toggled(self, checked: bool):
        """Toggle the visibility of the level meter and persist the setting."""
        self.config.show_level_meter = bool(checked)
        save_config(self.config)
        if self.recorder.is_recording and checked:
            self.level_meter.setVisible(True)
        elif not checked:
            self.level_meter.setVisible(False)

    def _on_voice_changed(self, _idx: int):
        """Persist the selected TTS voice and rebuild the announcer so the
        next announcement uses the new pack."""
        new_voice = self.voice_combo_main.currentData()
        if not new_voice or new_voice == self.config.tts_voice:
            return
        self.config.tts_voice = new_voice
        save_config(self.config)
        reset_announcer()
        self.status_label.setText(
            f"Voice: {self.voice_combo_main.currentText()}"
        )

    def _on_vad_toggled(self, checked: bool):
        """Persist VAD state when toggled from the main UI."""
        self.config.vad_enabled = bool(checked)
        save_config(self.config)
        status = "on" if checked else "off"
        self.status_label.setText(f"VAD {status}")

    def _on_silence_auto_stop(self):
        """Auto-stop callback fired after N seconds of silence post-speech."""
        if self.recorder.is_recording:
            self._stop_and_transcribe()

    def _tap_toggle(self):
        """Tap toggle: start recording, or stop and cache (for append workflow)."""
        if self.recorder.is_recording:
            self._stop_and_cache()
        else:
            self._start_recording()

    def _transcribe_cached(self):
        """Transcribe all cached audio segments."""
        if not self._cached_segments:
            self.status_label.setText("No cached audio to transcribe")
            return
        self._audio_feedback("play_transcribe", "announce_transcribing")
        audio_data = combine_wav_segments(self._cached_segments)
        self._cached_segments = []
        self._update_segment_indicator()
        self._transcribe(audio_data)

    def _send_transcribe(self):
        """Hotkey: transcribe (stopping recording first if active), then paste
        + Enter regardless of the configured output modes. The force-send flag
        is consumed by the delivery path in _transcribe_cached's downstream."""
        self._force_send_next = True
        if self.recorder.is_recording:
            self._stop_and_transcribe()
        elif self._cached_segments:
            self._transcribe_cached()
        else:
            # Nothing to send — clear the flag so a future paste isn't surprised.
            self._force_send_next = False
            self.status_label.setText("Send-transcribe: nothing recorded")

    def _start_append(self):
        """Start a new recording segment to append to cache."""
        self._start_append_recording()

    def _start_append_recording(self):
        """Start recording in append mode — new transcription appends to existing text."""
        self._append_mode = True
        self._start_recording()

    def _pause_resume(self):
        """Pause or resume recording."""
        if not self.recorder.is_recording:
            return
        if self.recorder.is_paused:
            self.recorder.resume_recording()
            self._audio_feedback("play_resume", "announce_resumed")
            self.pause_btn.setText("\u23f8  Pause")
            self.status_label.setText("Recording...")
        else:
            self.recorder.pause_recording()
            self._audio_feedback("play_pause", "announce_paused")
            self.pause_btn.setText("\u25b6  Resume")
            self.status_label.setText("Paused")

    def _retake(self):
        """Discard current recording and immediately start a new one."""
        if self.recorder.is_recording:
            self._duration_timer.stop()
            self.recorder.stop_recording()  # Discard the audio
            self._rec_store.clear_active()
        self._reset_record_buttons()
        self._audio_feedback("play_clear", "announce_cleared")
        self.status_label.setText("Retake...")
        self._start_recording()

    def _delete_recording(self):
        """Discard current recording and return to idle (no restart)."""
        if self.recorder.is_recording:
            self._duration_timer.stop()
            self.recorder.stop_recording()
            self._rec_store.clear_active()
        self._reset_record_buttons()
        self._audio_feedback("play_clear", "announce_discarded")
        self.duration_label.setText("")
        self.status_label.setText("Deleted")
        self._update_tray_state("idle")

    def _clear_recording(self):
        """Clear current recording and cache."""
        if self.recorder.is_recording:
            self._duration_timer.stop()
            self.recorder.stop_recording()
        self._reset_record_buttons()
        self._cached_segments = []
        self._update_segment_indicator()
        self._audio_feedback("play_clear", "announce_cleared")
        self.duration_label.setText("")
        self.status_label.setText("Cleared")
        self._update_tray_state("idle")

    def _update_duration(self):
        secs = self.recorder.get_duration()
        mins = int(secs) // 60
        sec = int(secs) % 60
        self.duration_label.setText(f"{mins}:{sec:02d}")

    # ── Transcription ──

    def _transcribe(self, audio_data: bytes, correction_notes: str = ""):
        if not self.config.openrouter_api_key:
            self.status_label.setText("No OpenRouter API key — open Settings")
            self._show_error_banner(
                "OpenRouter API key not set. Open Settings (Ctrl+,) to add one."
            )
            return

        # Cache for undo / re-transcribe
        self._last_raw_audio = audio_data
        self.retranscribe_action.setEnabled(True)
        if hasattr(self, "retry_btn"):
            self.retry_btn.setEnabled(True)

        # Stats: count recording seconds (from raw WAV)
        duration_seconds = 0.0
        try:
            from .audio_processor import get_audio_duration
            duration_seconds = get_audio_duration(audio_data)
            self._bump_record_seconds(duration_seconds)
        except Exception:
            pass

        # Persist to the on-disk recording store so a mid-transcription crash
        # doesn't lose the audio. Transcript is attached once the API returns.
        try:
            entry = self._rec_store.save_entry(
                audio_data,
                status="transcribing",
                duration_seconds=duration_seconds,
                model=self._effective_model(),
            )
            self._active_entry_id = entry.id
        except Exception:
            self._active_entry_id = None

        self._hide_error_banner()
        self.status_label.setText("Processing audio...")
        self.record_btn.setEnabled(False)
        self._update_tray_state("transcribing")

        # Build prompt with current UI settings (estimate duration from raw WAV)
        from .audio_processor import get_audio_duration
        prompt = build_cleanup_prompt(
            self.config,
            audio_duration_seconds=get_audio_duration(audio_data),
            correction_notes=correction_notes,
        )

        # Audio processing + transcription both run in background thread
        self.worker = TranscriptionWorker(
            api_key=self.config.openrouter_api_key,
            mistral_api_key=self.config.mistral_api_key,
            model=self._effective_model(),
            raw_audio_data=audio_data,
            prompt=prompt,
            review_enabled=self.config.review_enabled,
            vad_enabled=self.config.vad_enabled,
            provider=self.config.provider,
        )
        self.worker.status.connect(self.status_label.setText)
        self.worker.finished.connect(self._on_transcription_done)
        self.worker.error.connect(self._on_transcription_error)
        self.worker.start()

    def _on_transcription_done(self, text: str, elapsed: float):
        self.record_btn.setEnabled(True)
        self._update_tray_state("complete")

        # Persist transcript to on-disk store for this recording
        if self._active_entry_id:
            try:
                self._rec_store.attach_transcript(
                    self._active_entry_id,
                    text,
                    model=self._effective_model(),
                    elapsed_seconds=elapsed,
                )
            except Exception:
                pass
            self._active_entry_id = None

        # Stats: count sessions + words
        self._stats_sessions += 1
        self._stats_words += len((text or "").split())

        # Undo state: snapshot previous text before insert/append
        self._text_before_last_insert = self._raw_text
        self._last_inserted_text = text
        self.undo_insert_action.setEnabled(True)

        # Add to session history
        self._history.add(text, elapsed_seconds=elapsed,
                          format_preset=self.config.format_preset)
        self._refresh_history_list()

        # Append mode: join new text after existing with a blank line
        if self._append_mode and self._raw_text.strip():
            combined = self._raw_text.rstrip() + "\n\n" + text.lstrip()
            self._raw_text = combined
            self._append_mode = False
        else:
            self._raw_text = text
            self._append_mode = False

        # Append configured signature after a blank line, if enabled
        if self.config.output_append_signature and self.config.signature.strip():
            self._raw_text = self._raw_text.rstrip() + "\n\n" + self.config.signature.rstrip()

        if self.config.output_to_app:
            self.text_edit.setMarkdown(self._raw_text)
            cursor = self.text_edit.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.text_edit.setTextCursor(cursor)

        # Show Append button whenever there's text
        self.append_btn.setVisible(bool(self._raw_text.strip()))

        output_text = self._raw_text

        if self.config.output_to_clipboard:
            copy_to_clipboard(output_text)

        force_send = self._force_send_next
        self._force_send_next = False
        if force_send:
            # send_transcribe hotkey: paste + Enter regardless of inject setting.
            self._inject_text(output_text, force_enter=True)
        elif self.config.output_to_inject:
            self._inject_text(output_text)

        # Single "ready" cue — played once after delivery, regardless of
        # which output targets are enabled. Keeps the audio palette to two
        # clearly distinct events (start + ready).
        self._audio_feedback("play_ready", "announce_complete")

        # Status
        parts = [f"Done in {elapsed:.1f}s"]
        if self.config.output_to_clipboard:
            parts.append("On clipboard")
        if self.config.output_to_inject:
            parts.append("injected")
        self.status_label.setText(" | ".join(parts))

    def _on_transcription_error(self, error: str):
        self.record_btn.setEnabled(True)
        self.status_label.setText(f"Error: {error}")
        self._update_tray_state("idle")
        if self._active_entry_id:
            try:
                self._rec_store.mark_failed(self._active_entry_id, error)
            except Exception:
                pass
            self._active_entry_id = None
        hint = ""
        low = error.lower()
        if "401" in error or "403" in error or "auth" in low or "api key" in low:
            hint = "Check your API key in Settings (Ctrl+,)."
        elif "402" in error or "credit" in low:
            hint = "Out of credits — top up at openrouter.ai."
        elif "429" in error or "rate" in low:
            hint = "Rate limited — wait a moment and retry."
        elif "timeout" in low or "connection" in low or "network" in low:
            hint = "Network issue — check your internet connection."
        self._show_error_banner(f"Transcription failed: {error}" + (f"\n{hint}" if hint else ""))

    def _on_recording_error(self, error: str):
        self._duration_timer.stop()
        self.record_btn.setText("\u25cf  Record")
        self.record_btn.setStyleSheet(self._record_btn_style(False))
        self.status_label.setText(f"Mic error: {error}")

    # Window classes (lowercased) that use Ctrl+Shift+V for paste instead
    # of Ctrl+V. Terminals are the main case since Ctrl+V in a terminal
    # sends a literal control character instead of pasting.
    _TERMINAL_CLASSES = {
        "konsole", "yakuake", "xterm", "uxterm", "gnome-terminal",
        "gnome-terminal-server", "org.gnome.terminal", "alacritty",
        "kitty", "org.kde.konsole", "org.kde.yakuake", "wezterm",
        "wezterm-gui", "tilix", "terminator", "xfce4-terminal",
        "ptyxis", "foot", "footclient", "org.contour.contour",
        "rxvt", "urxvt", "st-256color", "ghostty",
    }

    def _detect_active_window_class(self) -> Optional[str]:
        """Return the active window's class name (lowercased), or None.

        Uses kdotool, which works under KDE Plasma on both Wayland-native
        and XWayland windows by talking to KWin via D-Bus.
        """
        try:
            wid_res = subprocess.run(
                ["kdotool", "getactivewindow"],
                capture_output=True, timeout=2, text=True,
            )
            if wid_res.returncode != 0:
                return None
            wid = wid_res.stdout.strip()
            if not wid:
                return None
            cls_res = subprocess.run(
                ["kdotool", "getwindowclassname", wid],
                capture_output=True, timeout=2, text=True,
            )
            if cls_res.returncode != 0:
                return None
            return cls_res.stdout.strip().lower() or None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        except Exception:
            return None

    def _paste_shortcut_for(self, window_class: Optional[str]) -> str:
        """Choose the right paste shortcut for the focused window.

        Terminals (Konsole, etc.) use Ctrl+Shift+V because Ctrl+V sends
        a literal control character. Everything else — Kate, browsers,
        IDEs, chat apps — uses plain Ctrl+V. (Ctrl+Shift+V in Kate
        opens the clipboard-history / paste-special dialog, which is
        why pastes were silently failing there.)
        """
        if window_class and any(t in window_class for t in self._TERMINAL_CLASSES):
            return "ctrl+shift+v"
        return "ctrl+v"

    def _inject_text(self, text: str, force_enter: bool = False):
        """Paste text at cursor position via clipboard + synthetic paste key.

        Works across both Wayland-native (Kate) and XWayland (Konsole) apps:
        KWin hands the ydotool-injected keystroke to whichever window has
        focus, regardless of protocol. The tricky part is that different
        apps bind paste to different shortcuts — so we detect the focused
        window class with kdotool and pick Ctrl+V or Ctrl+Shift+V
        accordingly.
        """
        try:
            # Save current clipboard so we can restore it after paste
            old_clip = None
            try:
                result = subprocess.run(
                    ["wl-paste", "--no-newline"],
                    capture_output=True, timeout=2,
                )
                if result.returncode == 0:
                    old_clip = result.stdout
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

            copy_to_clipboard(text)

            # Detect focused window and pick the correct paste shortcut.
            win_class = self._detect_active_window_class()
            shortcut = self._paste_shortcut_for(win_class)
            if os.environ.get("VOICE_TYPER_DEBUG"):
                print(
                    f"[paste] window_class={win_class} shortcut={shortcut}",
                    file=sys.stderr,
                )

            # ydotool 0.1.8 syntax: modifiers joined with + (no :state suffix)
            subprocess.run(
                ["ydotool", "key", shortcut],
                timeout=5, capture_output=True,
            )

            if force_enter or self.config.auto_press_enter_after_paste:
                # Brief gap so the paste lands before Enter; otherwise some
                # apps swallow the newline as part of the paste event.
                time.sleep(0.05)
                # ydotool key codes: 28 = KEY_ENTER. ":1" = press, ":0" = release.
                subprocess.run(
                    ["ydotool", "key", "28:1", "28:0"],
                    timeout=5, capture_output=True,
                )

            # Restore previous clipboard after paste completes
            if old_clip is not None:
                def _restore():
                    time.sleep(0.3)
                    try:
                        proc = subprocess.Popen(
                            ["wl-copy"], stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                        )
                        proc.communicate(input=old_clip, timeout=5)
                    except Exception:
                        pass
                threading.Thread(target=_restore, daemon=True).start()

        except FileNotFoundError:
            self.status_label.setText("ydotool not installed — can't inject text")
        except Exception as e:
            self.status_label.setText(f"Inject error: {e}")

    # ── UI Actions ──

    def _copy_text(self):
        text = self._raw_text if self._raw_text.strip() else self.text_edit.toPlainText()
        if text.strip():
            copy_to_clipboard(text)
            self.status_label.setText("Copied to clipboard")
        else:
            self.status_label.setText("Nothing to copy")

    def _clear_text(self):
        self._raw_text = ""
        self.text_edit.clear()
        self.append_btn.setVisible(False)
        self.status_label.setText("Cleared")

    def _on_text_context_menu(self, pos):
        menu = self.text_edit.createStandardContextMenu()
        cursor = self.text_edit.textCursor()
        if not cursor.hasSelection():
            # Auto-select the word under the cursor at the click position
            click_cursor = self.text_edit.cursorForPosition(pos)
            click_cursor.select(click_cursor.SelectionType.WordUnderCursor)
            selected = click_cursor.selectedText().strip()
        else:
            selected = cursor.selectedText().strip()

        if selected:
            menu.addSeparator()
            action = menu.addAction(f'Save "{selected[:40]}" as mistranscription…')
            action.triggered.connect(lambda: self._save_as_mistranscription(selected))

        menu.exec(self.text_edit.mapToGlobal(pos))

    def _save_as_mistranscription(self, wrong: str):
        from PyQt6.QtWidgets import QInputDialog
        correct, ok = QInputDialog.getText(
            self,
            "Save as mistranscription",
            f'Replace "{wrong}" with:',
            text=wrong,
        )
        if not ok:
            return
        correct = correct.strip()
        if not correct or correct == wrong:
            return

        entries = load_dict_entries()
        # Update existing entry with same `from` (case-insensitive), or append.
        replaced = False
        for e in entries:
            if e.get("from", "").lower() == wrong.lower():
                e["to"] = correct
                replaced = True
                break
        if not replaced:
            entries.append({
                "from": wrong,
                "to": correct,
                "whole_word": True,
                "case_sensitive": False,
            })
        try:
            save_dict_entries(entries)
        except Exception as e:
            QMessageBox.warning(self, "Save failed", f"Could not save dictionary:\n{e}")
            return

        # Apply the substitution to the current text immediately.
        current = self.text_edit.toPlainText()
        updated = apply_substitutions(current, [entries[-1] if not replaced else
                                                next(x for x in entries if x["from"].lower() == wrong.lower())])
        if updated != current:
            self.text_edit.setPlainText(updated)
            self._raw_text = updated

        self.status_label.setText(
            f'Saved: "{wrong}" → "{correct}"' + (" (updated)" if replaced else "")
        )

    def _on_format_changed(self):
        self.config.format_preset = self.format_combo.currentData()
        save_config(self.config)

    def _on_tone_changed(self):
        self.config.tone = self.tone_combo.currentData()
        save_config(self.config)

    def _on_clipboard_toggled(self, checked: bool):
        self.config.output_to_clipboard = checked
        save_config(self.config)

    def _on_inject_toggled(self, checked: bool):
        self.config.output_to_inject = checked
        save_config(self.config)

    def _on_auto_enter_toggled(self, checked: bool):
        self.config.auto_press_enter_after_paste = checked
        save_config(self.config)

    def _on_app_toggled(self, checked: bool):
        self.config.output_to_app = checked
        save_config(self.config)

    def _on_sig_toggled(self, checked: bool):
        self.config.output_append_signature = checked
        save_config(self.config)
        if checked and not self.config.signature.strip():
            self.status_label.setText(
                "Append signature on — but no signature set (Settings → Signature)"
            )

    def _toggle_app_mode(self):
        """Hotkey handler: flip Show-in-window mode."""
        self.app_check.setChecked(not self.app_check.isChecked())
        state = "on" if self.config.output_to_app else "off"
        self.status_label.setText(f"Window output {state}")

    def _toggle_clipboard_mode(self):
        """Hotkey handler: flip Clipboard mode."""
        self.clipboard_check.setChecked(not self.clipboard_check.isChecked())
        state = "on" if self.config.output_to_clipboard else "off"
        self.status_label.setText(f"Clipboard output {state}")

    def _toggle_inject_mode(self):
        """Hotkey handler: flip Type-at-cursor mode."""
        self.inject_check.setChecked(not self.inject_check.isChecked())
        state = "on" if self.config.output_to_inject else "off"
        self.status_label.setText(f"Type-at-cursor {state}")

    def _toggle_vad_mode(self):
        """Hotkey handler: flip VAD on/off."""
        if not self.vad_check_main.isEnabled():
            self.status_label.setText("VAD unavailable")
            return
        self.vad_check_main.setChecked(not self.vad_check_main.isChecked())

    def _toggle_meter_mode(self):
        """Hotkey handler: flip level-meter visibility."""
        self.show_meter_check.setChecked(not self.show_meter_check.isChecked())
        state = "on" if self.config.show_level_meter else "off"
        self.status_label.setText(f"Meter {state}")

    def _cycle_audio_feedback(self):
        """Cycle through audio feedback modes: beeps -> tts -> silent -> beeps."""
        modes = ["beeps", "tts", "silent"]
        idx = modes.index(self.config.audio_feedback_mode) if self.config.audio_feedback_mode in modes else 0
        self.config.audio_feedback_mode = modes[(idx + 1) % len(modes)]
        save_config(self.config)
        self._update_beep_indicator()

    def _update_beep_indicator(self):
        """Update the audio feedback indicator in status bar."""
        mode = self.config.audio_feedback_mode
        if mode == "beeps":
            self.beep_btn.setText("\U0001f514 Beeps")
        elif mode == "tts":
            self.beep_btn.setText("\U0001f50a Voice")
        else:
            self.beep_btn.setText("\U0001f507 Silent")

    # ── History ──

    def _toggle_history(self):
        """Toggle history list visibility (accordion)."""
        visible = not self.history_list.isVisible()
        self.history_list.setVisible(visible)
        self.history_toggle_btn.setText("▼ Recent" if visible else "▶ Recent")

    def _refresh_history_list(self):
        """Update the history list widget."""
        self.history_list.clear()
        for entry in self._history.get_all():
            item = QListWidgetItem(f"{entry.time_str}  {entry.preview}")
            item.setToolTip(entry.text[:500])
            item.setData(Qt.ItemDataRole.UserRole, entry.text)
            self.history_list.addItem(item)

    def _on_history_item_clicked(self, item: QListWidgetItem):
        """Load a history entry into the text editor."""
        text = item.data(Qt.ItemDataRole.UserRole)
        if text:
            self._raw_text = text
            self.text_edit.setMarkdown(text)
            self.status_label.setText("Loaded from history")

    def _clear_history(self):
        """Clear session history."""
        self._history.clear()
        self.history_list.clear()
        self.status_label.setText("History cleared")

    def _show_models_info(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Supported Models")
        dialog.setMinimumSize(560, 380)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 18, 20, 16)

        body = QLabel(
            "<h3>How this app uses models</h3>"
            "<p>Voxtype does <b>single-pass</b> transcription: "
            "audio goes to the model along with a cleanup prompt, and the "
            "model is expected to transcribe and format in one call. Only "
            "models that reliably handle this pattern are recommended.</p>"
            "<h3>Recommended</h3>"
            "<p><b>Gemini 3 Flash (Google)</b> — primary recommendation. "
            "Reliably follows the single-pass transcribe-and-format prompt "
            "without mistaking audio content for chat instructions.</p>"
            "<p><b>Gemini 2.5 Flash / Flash Lite (Google)</b> — solid "
            "alternatives if you prefer lower cost or slightly faster "
            "responses. Same robust single-pass behavior.</p>"
            "<h3>Not recommended</h3>"
            "<p><b>Voxtral (Mistral)</b> — fast and accurate at raw ASR, but "
            "in single-pass mode it frequently treats dictation content as a "
            "chat instruction (e.g. dictating \"write me a haiku\" produces a "
            "haiku instead of a transcription). This app does not do second-"
            "pass cleanup, so Voxtral is not a good fit here.</p>"
            "<p><b>GPT Audio / GPT-4o Audio (OpenAI)</b> — similar "
            "instruction-following failures; avoid unless you have a specific "
            "reason to test them.</p>"
            "<h3>Cost</h3>"
            "<p>Costs across the recommended Gemini models are very low — "
            "typically fractions of a cent per minute of dictation. Check "
            "<a href='https://openrouter.ai/models'>openrouter.ai/models</a> "
            "for current rates.</p>"
            "<p>All audio-capable models remain selectable from the main "
            "Model dropdown for experimentation.</p>"
        )
        body.setOpenExternalLinks(True)
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction
        )
        layout.addWidget(body)

        layout.addStretch()

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dialog.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

        dialog.exec()

    def _show_about(self):
        QMessageBox.about(
            self,
            "About Voxtype",
            f"<h3>Voxtype</h3>"
            f"<p>Version {APP_VERSION}</p>"
            f"<p><i>Multimodal AI transcription and reformatting with OpenRouter API</i></p>"
            f"<p>Voice dictation powered by multimodal AI models. "
            f"Audio is sent directly to audio-capable models which handle "
            f"both transcription and text cleanup in a single pass.</p>"
            f"<p><a href='https://openrouter.ai'>openrouter.ai</a></p>",
        )

    def _open_settings(self):
        dialog = SettingsDialog(self.config, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.config = dialog.get_config()
            save_config(self.config)
            self._populate_model_combo()
            self._update_translation_indicator()
            self._setup_hotkeys()
            # Refresh main-UI toggles to reflect settings changes
            for cb, val in (
                (self.vad_check_main, self.config.vad_enabled),
                (self.show_meter_check, self.config.show_level_meter),
                (self.sig_check, self.config.output_append_signature),
                (self.auto_enter_check, self.config.auto_press_enter_after_paste),
            ):
                cb.blockSignals(True)
                cb.setChecked(val)
                cb.blockSignals(False)
            if not self.config.show_level_meter:
                self.level_meter.setVisible(False)
            # Sync home-page voice picker + rebuild announcer with new pack
            idx = self.voice_combo_main.findData(self.config.tts_voice)
            if idx >= 0:
                self.voice_combo_main.blockSignals(True)
                self.voice_combo_main.setCurrentIndex(idx)
                self.voice_combo_main.blockSignals(False)
            reset_announcer()

    # ── Stats ──

    def _roll_stats_if_new_day(self):
        from datetime import date
        today = date.today()
        if today != self._stats_day:
            self._stats_day = today
            self._stats_sessions = 0
            self._stats_words = 0
            self._stats_record_seconds = 0.0
            self._stats_speak_seconds = 0.0

    def _bump_record_seconds(self, seconds: float):
        self._roll_stats_if_new_day()
        self._stats_record_seconds += max(0.0, seconds)
        self._stats_speak_seconds += max(0.0, seconds)

    # ── Error banner ──

    def _show_error_banner(self, text: str):
        self.error_banner.setText(text)
        self.error_banner.setVisible(True)

    def _hide_error_banner(self):
        self.error_banner.setVisible(False)
        self.error_banner.setText("")

    # ── Undo / re-transcribe ──

    def _undo_last_insert(self):
        if not self._last_inserted_text:
            return
        self._raw_text = self._text_before_last_insert
        self.text_edit.setMarkdown(self._raw_text)
        self._last_inserted_text = ""
        self.undo_insert_action.setEnabled(False)
        self.status_label.setText("Undid last insert")

    def _retranscribe_last(self):
        """Re-run transcription on the last raw audio with the currently selected model."""
        if not self._last_raw_audio:
            QMessageBox.information(self, "Re-transcribe",
                                    "No recent audio available.")
            return
        model_name = self._model_display_name(self._effective_model())
        ret = QMessageBox.question(
            self, "Re-transcribe",
            f"Re-transcribe the last recording with:\n\n  {model_name}\n\n"
            "Change the Model dropdown first if you want a different model.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
        )
        if ret != QMessageBox.StandardButton.Ok:
            return
        self._transcribe(self._last_raw_audio)

    def _retry_with_feedback(self):
        """Open the retry dialog and re-transcribe the last audio with notes."""
        if not self._last_raw_audio:
            QMessageBox.information(self, "Retry",
                                    "No recent audio available to retry.")
            return
        dialog = RetryDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        notes = dialog.feedback_text()
        self._transcribe(self._last_raw_audio, correction_notes=notes)

    # ── Usage page (session stats + per-key spend) ──

    def _show_usage(self):
        dlg = UsageDialog(self, api_key=self.config.openrouter_api_key,
                          session_stats=self._session_stats_dict())
        dlg.exec()

    def _show_recording_history(self):
        dlg = RecordingHistoryWindow(self._rec_store, self)
        dlg.retranscribe_requested.connect(self._retranscribe_from_file)
        dlg.exec()

    def _retranscribe_from_file(self, wav_path: str):
        try:
            with open(wav_path, "rb") as f:
                audio_data = f.read()
        except Exception as e:
            self._show_error_banner(f"Could not read audio file: {e}")
            return
        self._transcribe(audio_data)

    def _session_stats_dict(self) -> dict:
        self._roll_stats_if_new_day()
        wpm = 0
        if self._stats_speak_seconds > 0:
            wpm = int(self._stats_words / (self._stats_speak_seconds / 60.0))
        return {
            "day": self._stats_day.isoformat(),
            "sessions": self._stats_sessions,
            "words": self._stats_words,
            "record_seconds": self._stats_record_seconds,
            "wpm": wpm,
        }

    def _prompt_api_key(self):
        """Prompt for API key if not configured."""
        msg = QMessageBox(self)
        msg.setWindowTitle("API Key Required")
        msg.setText(
            "No OpenRouter API key found.\n\n"
            "You need an API key from openrouter.ai to use Voxtype.\n"
            "Open Settings to configure it."
        )
        msg.addButton("Open Settings", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        if msg.exec() == 0:
            self._open_settings()

    def closeEvent(self, event):
        # Save window size
        self.config.window_width = self.width()
        self.config.window_height = self.height()
        save_config(self.config)

        if self.hotkey_listener:
            self.hotkey_listener.stop()
        if self.recorder.is_recording:
            self.recorder.stop_recording()
        self.recorder.cleanup()
        event.accept()


def _acquire_single_instance_lock():
    """Take an exclusive flock on a runtime file. Returns the open fd on success,
    or None if another instance already holds the lock. The fd is intentionally
    kept open for process lifetime — closing it would release the lock."""
    import fcntl
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/voxtype-{os.getuid()}"
    try:
        os.makedirs(runtime_dir, exist_ok=True)
    except OSError:
        runtime_dir = "/tmp"
    lock_path = os.path.join(runtime_dir, "voxtype.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    return fd


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Voxtype")
    app.setDesktopFileName("ai-typer-v2")

    lock_fd = _acquire_single_instance_lock()
    if lock_fd is None:
        QMessageBox.warning(
            None,
            "Voxtype already running",
            "Another instance of Voxtype is already running.\n\n"
            "Look for the microphone icon in your system tray. "
            "Running two copies at once would cause hotkey conflicts and "
            "duplicate transcriptions, so this instance will exit.",
        )
        sys.exit(0)
    app._voxtype_lock_fd = lock_fd  # keep reference so GC doesn't close it

    # App icon — prefer system-installed hicolor icon, fall back to bundled asset
    icon = QIcon.fromTheme("ai-typer-v2")
    if icon.isNull():
        bundled = Path(__file__).parent.parent / "assets" / "icon.png"
        if bundled.exists():
            icon = QIcon(str(bundled))
    if not icon.isNull():
        app.setWindowIcon(icon)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
