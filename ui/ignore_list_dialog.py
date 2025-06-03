# ui/ignore_list_dialog.py
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QLineEdit,
    QPushButton, QMessageBox, QDialogButtonBox
)
from typing import List

class IgnoreListDialog(QDialog):
    def __init__(self, current_patterns: List[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Ignore List")
        self.setMinimumWidth(400)

        self.current_patterns = list(current_patterns) # Work on a copy

        layout = QVBoxLayout(self)

        self.patterns_list_widget = QListWidget()
        self.patterns_list_widget.addItems(self.current_patterns)
        layout.addWidget(self.patterns_list_widget)

        # Add/Remove controls
        controls_layout = QHBoxLayout()
        self.pattern_input = QLineEdit()
        self.pattern_input.setPlaceholderText("e.g., *.tmp or folder_name/")
        controls_layout.addWidget(self.pattern_input)

        add_button = QPushButton("Add")
        add_button.clicked.connect(self.add_pattern)
        controls_layout.addWidget(add_button)
        layout.addLayout(controls_layout)

        remove_button = QPushButton("Remove Selected")
        remove_button.clicked.connect(self.remove_pattern)
        layout.addWidget(remove_button)

        # Dialog buttons (OK/Cancel)
        button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def add_pattern(self):
        pattern_text = self.pattern_input.text().strip()
        if not pattern_text:
            QMessageBox.warning(self, "Empty Pattern", "Pattern cannot be empty.")
            return
        if pattern_text in self.current_patterns:
            QMessageBox.information(self, "Duplicate", "Pattern already in list.")
            return
        
        self.current_patterns.append(pattern_text)
        self.patterns_list_widget.addItem(pattern_text)
        self.pattern_input.clear()

    def remove_pattern(self):
        selected_items = self.patterns_list_widget.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "No Selection", "Please select a pattern to remove.")
            return
        
        for item in selected_items:
            pattern_to_remove = item.text()
            if pattern_to_remove in self.current_patterns:
                self.current_patterns.remove(pattern_to_remove)
            self.patterns_list_widget.takeItem(self.patterns_list_widget.row(item))

    def get_updated_patterns(self) -> List[str]:
        return self.current_patterns