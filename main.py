import json
import os
import sys

from PyQt6.QtCore import Qt
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
        (your example format)
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
        # Skip whitespace and commas between values
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

    # If multiple values were parsed, interpret as a list of entries
    if len(values) > 1:
        return values

    # If exactly one value parsed, ensure the remainder is only ignorable separators
    remainder = text[i:].strip(" \t\r\n,")
    if remainder:
        raise ValueError("Parsed one JSON value, but extra unexpected content remains in the file.")

    return values[0]


class JsonOrganizer(QDialog):
    """
    Displays top-level JSON entries as one row each.
    Drag-drop rows to reorder.
    Save the reordered JSON.
    """

    def __init__(self, data, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Reorder: {title}")
        self.resize(900, 600)

        self._original_type = None  # "list" or "dict"
        self._title = title

        layout = QVBoxLayout(self)

        self.info_label = QLabel(self)
        layout.addWidget(self.info_label)

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
                "Top-level JSON type: list (each element is one row). Drag to reorder.\n"
                "Row display: shows only the 'name' field when present."
            )
            for element in data:
                item = QListWidgetItem(row_label(element))
                item.setData(Qt.ItemDataRole.UserRole, element)
                self.list_widget.addItem(item)

        elif isinstance(data, dict):
            self._original_type = "dict"
            self.info_label.setText(
                "Top-level JSON type: object/dict (each key-value pair is one row). Drag to reorder.\n"
                "Row display: shows only the value's 'name' field when present."
            )
            for k, v in data.items():
                # Display only the nested name if available; otherwise show key + compact value.
                display = row_label(v)
                if display == "" or display == compact_json(v):
                    # fallback keeps the key visible
                    display = f"{k}: {compact_json(v)}"
                item = QListWidgetItem(display)

                # Store (key, value) so we can rebuild in the new order
                item.setData(Qt.ItemDataRole.UserRole, (k, v))
                self.list_widget.addItem(item)

        else:
            self._original_type = "other"
            self.info_label.setText("Top-level JSON is not a list or object; cannot reorder as rows.")
            item = QListWidgetItem(compact_json(data))
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDragEnabled)
            self.list_widget.addItem(item)
            self.save_as_btn.setEnabled(False)

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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(500, 300)
        self.setWindowTitle("JSON Organizer Tool")

        button = QPushButton("Load JSON")
        button.clicked.connect(self.load_json)
        self.setCentralWidget(button)

        # Keep dialogs alive
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