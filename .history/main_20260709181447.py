import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Optional, Tuple

import requests
from PyQt6.QtCore import Qt, QObject, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QBrush
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QPushButton,
    QFileDialog,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QLabel,
)


WORKSHOP_URL_TEMPLATE = "https://reforger.armaplatform.com/workshop/{mod_id}"
HTTP_TIMEOUT_SECONDS = 15


def compact_json(value) -> str:
    """Small, single-line-ish representation for fallback row display."""
    try:
        s = json.dumps(value, ensure_ascii=False)
    except Exception:
        s = str(value)
    if len(s) > 300:
        s = s[:297] + "..."
    return s


def row_label(value) -> str:
    """
    What the user sees in the list.
    If it's a dict with a 'name' key, show only that.
    Otherwise, fall back to compact JSON.
    """
    if isinstance(value, dict) and "name" in value:
        return str(value.get("name", ""))
    return compact_json(value)


def load_json_flexible(file_path: str):
    """
    Loads:
      - Normal JSON: list/dict/etc.
      - OR files containing multiple top-level JSON values separated by commas/whitespace:
            { ... },
            { ... },
            { ... }
    Uses a streaming-style decode loop via JSONDecoder.raw_decode().
    """
    with open(file_path, "r", encoding="utf-8-sig") as f:
        raw = f.read()

    text = raw.strip()
    if not text:
        raise ValueError("File is empty.")

    # 1) Try normal JSON first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2) Try "multiple JSON values" parsing (robust for {..}, {..}, ...)
    dec = json.JSONDecoder()
    i = 0
    n = len(text)
    values = []

    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break

        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError as e:
            snippet = text[max(0, i - 60) : min(n, i + 120)]
            raise ValueError(
                "Couldn't parse JSON near:\n"
                f"{snippet}\n\n"
                "This file does not appear to be valid JSON. If it's intended to be a list of objects, "
                "ensure each object is valid JSON and separated by commas."
            ) from e

        values.append(obj)
        i = end

    if not values:
        raise ValueError("Could not parse any JSON values from the file.")

    if len(values) > 1:
        return values

    remainder = text[i:].strip(" \t\r\n,")
    if remainder:
        raise ValueError("Parsed one JSON value, but extra unexpected content remains in the file.")

    return values[0]


# -------------------- Version checking logic --------------------

# Matches EXACTLY this structure in the raw workshop page HTML:
#
#     <dt class="py-3.5 font-bold leading-none">Version</dt>
#     <dd class="flex items-center gap-1">1.2.1</dd>
#
# Key points:
#   - The <dt> inner text must be exactly "Version" (nothing else), which
#     automatically excludes the "Game Version" and "Version size" rows,
#     since their <dt> text is not exactly "Version".
#   - The version number is taken ONLY from the <dd> that immediately
#     follows that <dt>.
#   - Attribute values are not hardcoded, so minor site CSS/class changes
#     won't break the match; only the dt/dd structure and label matter.
VERSION_DT_DD_RE = re.compile(
    r"<dt\b[^>]*>\s*Version\s*</dt>\s*<dd\b[^>]*>\s*([0-9]+(?:\.[0-9]+)*)\s*</dd>",
    re.IGNORECASE | re.DOTALL,
)


def fetch_workshop_version(mod_id: str, session: requests.Session) -> Optional[str]:
    """
    Extracts the MOD version from the workshop page.

    Looks ONLY for the metadata row whose <dt> label is exactly 'Version'
    and returns the number inside the <dd> immediately following it.
    'Game Version' and 'Version size' rows are never matched because their
    <dt> text is not exactly 'Version'.
    """
    url = WORKSHOP_URL_TEMPLATE.format(mod_id=mod_id)
    headers = {
        "User-Agent": "JSON-Organizer-Tool/2.1 (+version-check)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    r = session.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    r.raise_for_status()

    m = VERSION_DT_DD_RE.search(r.text)
    if not m:
        return None

    return m.group(1)


def _tokenize_version(v: str) -> Tuple:
    parts = v.strip().split(".")
    tokens = []
    for p in parts:
        if p.isdigit():
            tokens.append((0, int(p)))
        else:
            m = re.match(r"^(\d+)(.*)$", p)
            if m:
                tokens.append((0, int(m.group(1))))
                tail = m.group(2)
                if tail:
                    tokens.append((1, tail))
            else:
                tokens.append((1, p))
    return tuple(tokens)


def is_version_lower(local: str, remote: str) -> bool:
    try:
        return _tokenize_version(local) < _tokenize_version(remote)
    except Exception:
        return str(local) < str(remote)


@dataclass
class UpdateResult:
    index: int
    mod_id: str
    name: str
    old_version: str
    new_version: str
    updated: bool
    error: Optional[str] = None


class VersionWorker(QObject):
    progress = pyqtSignal(int, int)  # done, total
    result = pyqtSignal(object)      # UpdateResult
    finished = pyqtSignal()

    def __init__(self, entries: list[dict]):
        super().__init__()
        self.entries = entries
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        total = len(self.entries)
        done = 0

        with requests.Session() as session:
            for idx, entry in enumerate(self.entries):
                if self._stop:
                    break

                try:
                    mod_id = str(entry.get("modId", "")).strip()
                    name = str(entry.get("name", "")).strip()
                    old_v = str(entry.get("version", "")).strip()

                    if not mod_id or not old_v:
                        raise ValueError("Missing modId or version in this entry.")

                    remote_v = fetch_workshop_version(mod_id, session)
                    if not remote_v:
                        raise ValueError(
                            "Could not find the <dt>Version</dt>/<dd> row on the workshop page."
                        )

                    updated = is_version_lower(old_v, remote_v)

                    self.result.emit(
                        UpdateResult(
                            index=idx,
                            mod_id=mod_id,
                            name=name,
                            old_version=old_v,
                            new_version=remote_v,
                            updated=updated,
                        )
                    )

                except Exception as e:
                    mod_id = str(entry.get("modId", "")).strip() if isinstance(entry, dict) else ""
                    name = str(entry.get("name", "")).strip() if isinstance(entry, dict) else ""
                    old_v = str(entry.get("version", "")).strip() if isinstance(entry, dict) else ""
                    self.result.emit(
                        UpdateResult(
                            index=idx,
                            mod_id=mod_id,
                            name=name,
                            old_version=old_v,
                            new_version=old_v,
                            updated=False,
                            error=str(e),
                        )
                    )

                done += 1
                self.progress.emit(done, total)

        self.finished.emit()


# -------------------- UI --------------------

class JsonOrganizer(QDialog):
    """
    Displays top-level JSON entries as one row each.
    Drag-drop rows to reorder.
    Save the reordered JSON.
    Also can check workshop versions by modId and update entries.
    """

    def __init__(self, data, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Reorder: {title}")
        self.resize(900, 600)

        self._original_type = None  # "list" or "dict"
        self._title = title

        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[VersionWorker] = None

        layout = QVBoxLayout(self)

        self.info_label = QLabel(self)
        layout.addWidget(self.info_label)

        self.progress_label = QLabel(self)
        self.progress_label.setText("")
        layout.addWidget(self.progress_label)

        self.list_widget = QListWidget(self)
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)

        # Enable dragging rows up/down
        self.list_widget.setDragEnabled(True)
        self.list_widget.setAcceptDrops(True)
        self.list_widget.setDropIndicatorShown(True)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)

        layout.addWidget(self.list_widget)

        btn_row = QHBoxLayout()

        self.check_versions_btn = QPushButton("Check Workshop Versions", self)
        self.check_versions_btn.clicked.connect(self.check_versions)
        btn_row.addWidget(self.check_versions_btn)

        self.save_as_btn = QPushButton("Save As…", self)
        self.save_as_btn.clicked.connect(self.save_as)
        btn_row.addWidget(self.save_as_btn)

        self.close_btn = QPushButton("Close", self)
        self.close_btn.clicked.connect(self.close)
        btn_row.addWidget(self.close_btn)

        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.load_data(data)

    def load_data(self, data):
        self.list_widget.clear()

        if isinstance(data, list):
            self._original_type = "list"
            self.info_label.setText(
                "Drag to reorder.\n"
                "Use 'Check Workshop Versions' to update versions."
            )
            for element in data:
                item = QListWidgetItem(row_label(element))
                item.setData(Qt.ItemDataRole.UserRole, element)
                self.list_widget.addItem(item)

            self.check_versions_btn.setEnabled(True)

        elif isinstance(data, dict):
            self._original_type = "dict"
            self.info_label.setText(
                "Top-level JSON type: object/dict (each key-value pair is one row). Drag to reorder.\n"
                "Version checking is only supported for a top-level list of mod entries."
            )
            for k, v in data.items():
                display = row_label(v)
                if display == "" or display == compact_json(v):
                    display = f"{k}: {compact_json(v)}"
                item = QListWidgetItem(display)
                item.setData(Qt.ItemDataRole.UserRole, (k, v))
                self.list_widget.addItem(item)

            self.check_versions_btn.setEnabled(False)

        else:
            self._original_type = "other"
            self.info_label.setText("Top-level JSON is not a list or object; cannot reorder as rows.")
            item = QListWidgetItem(compact_json(data))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            self.list_widget.addItem(item)
            self.save_as_btn.setEnabled(False)
            self.check_versions_btn.setEnabled(False)

    def get_reordered_data(self):
        if self._original_type == "list":
            out = []
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                out.append(item.data(Qt.ItemDataRole.UserRole))
            return out

        if self._original_type == "dict":
            out = {}
            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                k, v = item.data(Qt.ItemDataRole.UserRole)
                out[k] = v
            return out

        return None

    def save_as(self):
        data = self.get_reordered_data()
        if data is None:
            QMessageBox.warning(self, "Cannot Save", "This JSON type is not supported for row reordering.")
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Reordered JSON As",
            os.path.splitext(self._title)[0] + "_reordered.json",
            "JSON Files (*.json);;All Files (*)",
        )
        if not file_path:
            return

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            QMessageBox.information(self, "Saved", f"Saved reordered JSON to:\n{file_path}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", str(e))

    def _set_row_status_updated(self, row: int, base_name: str, old_v: str, new_v: str):
        item = self.list_widget.item(row)
        item.setText(f"{base_name}  ✅ UPDATED {old_v} → {new_v}")
        item.setForeground(QBrush(QColor(0, 140, 0)))

    def _set_row_status_ok(self, row: int, base_name: str, v: str):
        item = self.list_widget.item(row)
        item.setText(f"{base_name}  • up-to-date ({v})")
        item.setForeground(QBrush(QColor(0, 0, 0)))

    def _set_row_status_error(self, row: int, base_name: str, msg: str):
        item = self.list_widget.item(row)
        item.setText(f"{base_name}  ⚠ ERROR: {msg}")
        item.setForeground(QBrush(QColor(180, 0, 0)))

    def check_versions(self):
        if self._original_type != "list":
            QMessageBox.warning(self, "Not Supported", "Version checking is only supported for a top-level JSON list.")
            return

        # Disable drag/reorder during check to keep row indices aligned
        self.list_widget.setDragDropMode(QListWidget.DragDropMode.NoDragDrop)

        # Gather entries by current row order (we will update the item payloads on results)
        entries = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            val = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(val, dict):
                entries.append(val)
            else:
                entries.append({"name": row_label(val), "modId": "", "version": ""})

        self.check_versions_btn.setEnabled(False)
        self.progress_label.setText("Checking workshop versions…")

        self._worker_thread = QThread(self)
        self._worker = VersionWorker(entries)
        self._worker.moveToThread(self._worker_thread)

        self._worker_thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_version_progress)
        self._worker.result.connect(self._on_version_result)
        self._worker.finished.connect(self._on_version_finished)
        self._worker.finished.connect(self._worker_thread.quit)

        self._worker_thread.start()

    def _on_version_progress(self, done: int, total: int):
        self.progress_label.setText(f"Checking workshop versions… {done}/{total}")

    def _on_version_result(self, res: UpdateResult):
        row = res.index
        if row < 0 or row >= self.list_widget.count():
            return

        item = self.list_widget.item(row)
        entry = item.data(Qt.ItemDataRole.UserRole)

        # Base name for display
        base_name = ""
        if isinstance(entry, dict):
            base_name = row_label(entry) or res.name or (res.mod_id or f"Row {row+1}")
        else:
            base_name = row_label(entry)

        if res.error:
            self._set_row_status_error(row, base_name, res.error)
            return

        # ✅ CRITICAL FIX:
        # Persist the updated version back into the item's stored data so Save As writes it.
        if isinstance(entry, dict) and res.updated:
            entry["version"] = res.new_version
            item.setData(Qt.ItemDataRole.UserRole, entry)

        if res.updated:
            self._set_row_status_updated(row, base_name, res.old_version, res.new_version)
        else:
            self._set_row_status_ok(row, base_name, res.new_version)

    def _on_version_finished(self):
        self.progress_label.setText("Version check complete.")
        self.check_versions_btn.setEnabled(True)

        # Re-enable drag/reorder after check
        self.list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)

        self._worker = None
        self._worker_thread = None


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(500, 300)
        self.setWindowTitle("JSON Organizer Tool")

        button = QPushButton("Load JSON")
        button.clicked.connect(self.load_json)
        self.setCentralWidget(button)

        self._dialogs = []

    def load_json(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open JSON File",
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if not file_path:
            return

        try:
            data = load_json_flexible(file_path)
            dlg = JsonOrganizer(data, os.path.basename(file_path), parent=self)
            dlg.show()
            self._dialogs.append(dlg)
        except Exception as e:
            QMessageBox.critical(self, "Error loading JSON", str(e))


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()