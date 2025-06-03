# ui/main_window.py
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QComboBox, QFileDialog, QMessageBox, QTreeWidget,
    QTreeWidgetItem, QLineEdit, QLabel, QProgressBar, QAbstractItemView,
    QMenuBar
)
from PyQt6.QtCore import Qt, QUrl 
from PyQt6.QtGui import QAction 
from typing import Dict, List 

from core.db_manager import DatabaseManager
from core.snapshot_manager import SnapshotWorker
from core.comparison_engine import ComparisonWorker
from utils.config_handler import ConfigManager 
from ui.ignore_list_dialog import IgnoreListDialog 

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Folder Snapshot & Change Tracker")
        self.setGeometry(100, 100, 1000, 700)

        self.db_manager = DatabaseManager()
        self.config_manager = ConfigManager() 
        self.selected_folders_for_snapshot = []
        self.snapshot_worker: SnapshotWorker = None # Type hinting for clarity
        self.comparison_worker: ComparisonWorker = None # Type hinting

        self._create_menu_bar() 
        self.init_ui()
        self.load_snapshots_into_combo()

    def _create_menu_bar(self): 
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")

        settings_action = QAction("&Settings...", self)
        settings_action.triggered.connect(self.open_settings_dialog)
        file_menu.addAction(settings_action)
        
        file_menu.addSeparator()
        
        exit_action = QAction("&Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    def open_settings_dialog(self): 
        current_patterns = self.config_manager.get_ignore_patterns()
        dialog = IgnoreListDialog(current_patterns, self)
        if dialog.exec(): 
            updated_patterns = dialog.get_updated_patterns()
            self.config_manager.set_ignore_patterns(updated_patterns)
            QMessageBox.information(self, "Settings Saved", "Ignore list updated.")


    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # Left Panel: Folder Selection & Snapshot Actions
        left_panel = QVBoxLayout()
        
        self.folder_list_widget = QListWidget()
        left_panel.addWidget(QLabel("Folders to Snapshot:"))
        left_panel.addWidget(self.folder_list_widget)

        add_folder_button = QPushButton("Add Folder") 
        add_folder_button.clicked.connect(self.add_folder) 
        left_panel.addWidget(add_folder_button)

        remove_folder_button = QPushButton("Remove Selected Folder")
        remove_folder_button.clicked.connect(self.remove_selected_folders)
        left_panel.addWidget(remove_folder_button)

        clear_folders_button = QPushButton("Clear All Folders")
        clear_folders_button.clicked.connect(self.clear_all_folders)
        left_panel.addWidget(clear_folders_button)

        self.snapshot_name_input = QLineEdit()
        self.snapshot_name_input.setPlaceholderText("Optional: Snapshot Name (auto-generated if empty)")
        left_panel.addWidget(QLabel("Snapshot Name:"))
        left_panel.addWidget(self.snapshot_name_input)

        create_snapshot_button = QPushButton("Create Snapshot")
        create_snapshot_button.clicked.connect(self.create_snapshot)
        left_panel.addWidget(create_snapshot_button)
        
        left_panel.addSpacing(20)

        self.snapshots_combo = QComboBox()
        left_panel.addWidget(QLabel("Select Snapshot to Compare/Delete:"))
        left_panel.addWidget(self.snapshots_combo)

        compare_button = QPushButton("Compare with Current State")
        compare_button.clicked.connect(self.compare_snapshot)
        left_panel.addWidget(compare_button)
        
        delete_snapshot_button = QPushButton("Delete Selected Snapshot")
        delete_snapshot_button.clicked.connect(self.delete_snapshot)
        left_panel.addWidget(delete_snapshot_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        left_panel.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        left_panel.addWidget(self.status_label)

        self.cancel_button = QPushButton("Cancel Operation")
        self.cancel_button.clicked.connect(self.cancel_current_operation)
        self.cancel_button.setVisible(False)
        left_panel.addWidget(self.cancel_button)

        left_panel.addStretch()
        main_layout.addLayout(left_panel, 1) 

        # Right Panel: Change Display
        right_panel = QVBoxLayout()
        self.results_tree = QTreeWidget()
        self.results_tree.setColumnCount(4) 
        self.results_tree.setHeaderLabels(["Status", "Name", "Relative Path", "Details (Size/LMT/Hash)"])
        right_panel.addWidget(QLabel("Comparison Results:"))
        right_panel.addWidget(self.results_tree)
        main_layout.addLayout(right_panel, 3)

    def add_folder(self): 
        folder_path = QFileDialog.getExistingDirectory(self, "Select Folder to Monitor")
        if folder_path: 
            if folder_path not in self.selected_folders_for_snapshot: 
                self.selected_folders_for_snapshot.append(folder_path)
                self.folder_list_widget.addItem(folder_path)
    
    def remove_selected_folders(self):
        selected_items = self.folder_list_widget.selectedItems()
        if not selected_items: return
        for item in selected_items: 
            path_to_remove = item.text()
            if path_to_remove in self.selected_folders_for_snapshot:
                self.selected_folders_for_snapshot.remove(path_to_remove)
            self.folder_list_widget.takeItem(self.folder_list_widget.row(item))

    def clear_all_folders(self):
        self.selected_folders_for_snapshot.clear()
        self.folder_list_widget.clear()

    def load_snapshots_into_combo(self):
        self.snapshots_combo.clear()
        snapshots = self.db_manager.get_all_snapshots()
        if not snapshots:
            self.snapshots_combo.addItem("No snapshots available", -1)
            return
        for snap in snapshots:
            self.snapshots_combo.addItem(f"{snap['name']} ({snap['timestamp']})", snap['id'])

    def cancel_current_operation(self):
        self.status_label.setText("Cancellation requested...")
        self.cancel_button.setEnabled(False) # Prevent multiple clicks
        if self.snapshot_worker and self.snapshot_worker.isRunning():
            self.snapshot_worker.request_cancellation()
        elif self.comparison_worker and self.comparison_worker.isRunning():
            self.comparison_worker.request_cancellation()
        else:
            self.status_label.setText("No active operation to cancel.")
            self.cancel_button.setVisible(False)


    def create_snapshot(self):
        if not self.selected_folders_for_snapshot:
            QMessageBox.warning(self, "No Folders", "Please add at least one folder to create a snapshot.")
            return
        
        if self.snapshot_worker and self.snapshot_worker.isRunning():
            QMessageBox.information(self, "Busy", "A snapshot operation is already in progress.")
            return

        snapshot_name = self.snapshot_name_input.text().strip()
        ignore_patterns = self.config_manager.get_ignore_patterns()
        
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0,0) # Indeterminate initially
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.status_label.setText("Starting snapshot creation...")
        self.set_ui_for_operation(True) # Disable most UI

        self.snapshot_worker = SnapshotWorker(
            list(self.selected_folders_for_snapshot), 
            snapshot_name,
            ignore_patterns 
        )
        self.snapshot_worker.progress_updated.connect(self.update_progress)
        self.snapshot_worker.snapshot_complete.connect(self.on_snapshot_complete)
        self.snapshot_worker.snapshot_error.connect(self.on_operation_error)
        self.snapshot_worker.finished.connect(self.on_worker_finished) # Generic finished signal
        self.snapshot_worker.start()

    def compare_snapshot(self):
        snapshot_id = self.snapshots_combo.currentData()
        if snapshot_id == -1 or snapshot_id is None: 
            QMessageBox.warning(self, "No Snapshot", "Please select a snapshot to compare.")
            return

        if self.comparison_worker and self.comparison_worker.isRunning():
            QMessageBox.information(self, "Busy", "A comparison operation is already in progress.")
            return

        ignore_patterns = self.config_manager.get_ignore_patterns() 

        self.results_tree.clear()
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0,0) 
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.status_label.setText("Starting comparison...")
        self.set_ui_for_operation(True) # Disable most UI

        self.comparison_worker = ComparisonWorker(snapshot_id, ignore_patterns)
        self.comparison_worker.comparison_progress.connect(self.update_progress)
        self.comparison_worker.comparison_complete.connect(self.on_comparison_complete)
        self.comparison_worker.comparison_error.connect(self.on_operation_error)
        self.comparison_worker.finished.connect(self.on_worker_finished) # Generic finished signal
        self.comparison_worker.start()

    def delete_snapshot(self):
        snapshot_id = self.snapshots_combo.currentData()
        if snapshot_id == -1 or snapshot_id is None:
            QMessageBox.warning(self, "No Snapshot", "Please select a snapshot to delete.")
            return
        
        snap_name = self.snapshots_combo.currentText()
        reply = QMessageBox.question(self, "Confirm Delete",
                                     f"Are you sure you want to delete snapshot: {snap_name}?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.db_manager.delete_snapshot(snapshot_id)
            self.load_snapshots_into_combo()
            QMessageBox.information(self, "Deleted", f"Snapshot '{snap_name}' deleted.")
            self.results_tree.clear() 

    def update_progress(self, current, total, message):
        if total > 0 and current <= total : # Determinate
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
        elif total == 0 and current == 0: # Special case for indeterminate from worker
            self.progress_bar.setRange(0,0)
        else: # Could be just a message update, or estimation phase
            self.progress_bar.setRange(0,0) # Fallback to indeterminate
        self.status_label.setText(message)

    def on_snapshot_complete(self, snapshot_id, name):
        self.status_label.setText(f"Snapshot '{name}' created successfully (ID: {snapshot_id}).")
        self.load_snapshots_into_combo()
        self.snapshot_name_input.clear() 
        QMessageBox.information(self, "Snapshot Complete", f"Snapshot '{name}' created.")
        # UI re-enabling will be handled by on_worker_finished

    def on_comparison_complete(self, results: Dict[str, List]):
        self.status_label.setText("Comparison complete.")
        self.results_tree.clear()
        # ... (Tree population logic as before - I'll keep it concise here for brevity)
        categories = {
            "Added": results.get('added', []), "Deleted": results.get('deleted', []),
            "Modified": results.get('modified', []) 
        }
        for cat_name, items in categories.items():
            cat_item = QTreeWidgetItem(self.results_tree, [cat_name, f"({len(items)} items)"])
            for item_data in items:
                # ... (Detailed item display logic from previous version) ...
                display_name = ""
                display_rel_path = ""
                display_details_list = []

                if cat_name == "Modified":
                    new_item_info = item_data['new']
                    old_item_info = item_data['old']
                    display_name = new_item_info['item_name']
                    display_rel_path = new_item_info['relative_path']
                    
                    if new_item_info['is_file'] != old_item_info['is_file']:
                        display_details_list.append(f"Type changed (File<->Folder)")
                    elif new_item_info['is_file']: 
                        if new_item_info.get('size') != old_item_info.get('size'):
                            display_details_list.append(f"Size: {old_item_info.get('size', 'N/A')} -> {new_item_info.get('size', 'N/A')}")
                        if new_item_info.get('lmt') != old_item_info.get('lmt'):
                            display_details_list.append(f"LMT changed")
                        oh = old_item_info.get('content_hash', 'N/A')
                        nh = new_item_info.get('content_hash', 'N/A')
                        if oh != nh:
                             display_details_list.append(f"Hash: {str(oh)[:8]}... -> {str(nh)[:8]}...")
                    else: 
                        display_details_list.append("Folder metadata/content changed")
                else: 
                    display_name = item_data['item_name']
                    display_rel_path = item_data['relative_path']
                    if item_data['is_file']:
                        display_details_list.append(f"Size: {item_data.get('size', 'N/A')}")
                        h = item_data.get('content_hash', 'N/A')
                        if h and not str(h).startswith("ERROR") and h != "CANCELLED_HASH_LIVE":
                            display_details_list.append(f"Hash: {str(h)[:8]}...")
                        elif h:
                             display_details_list.append(f"Hash: {h}")
                details_str = "; ".join(display_details_list) if display_details_list else "N/A"
                QTreeWidgetItem(cat_item, ["", display_name, display_rel_path, details_str])
            self.results_tree.expandItem(cat_item)
        for i in range(self.results_tree.columnCount()):
            self.results_tree.resizeColumnToContents(i)
        # UI re-enabling will be handled by on_worker_finished

    def on_operation_error(self, error_message):
        # This will be displayed, and then on_worker_finished will re-enable UI
        if "cancelled" in error_message.lower():
             self.status_label.setText(f"Operation Cancelled: {error_message.replace('Operation cancelled: ', '').replace('Operation cancelled', '')}")
             QMessageBox.information(self, "Operation Cancelled", error_message)
        else:
            self.status_label.setText(f"Error: {error_message}")
            QMessageBox.critical(self, "Operation Error", error_message)
        # UI re-enabling will be handled by on_worker_finished

    def on_worker_finished(self):
        """Generic slot called when either worker's QThread.finished signal is emitted."""
        self.progress_bar.setVisible(False)
        self.cancel_button.setVisible(False)
        self.cancel_button.setEnabled(True) # Re-enable for next operation
        self.set_ui_for_operation(False) # Re-enable UI

        # Clear worker references
        if self.sender() == self.snapshot_worker:
            self.snapshot_worker = None
        elif self.sender() == self.comparison_worker:
            self.comparison_worker = None


    def set_ui_for_operation(self, is_running: bool):
        """Disables/Enables UI elements during long operations."""
        enabled = not is_running
        # Disable all buttons except the cancel button if an operation is running
        for button in self.findChildren(QPushButton):
            if button == self.cancel_button:
                button.setVisible(is_running) # Show/hide cancel button
                button.setEnabled(is_running) # Enable cancel button only when running
            else:
                button.setEnabled(enabled)
        
        self.folder_list_widget.setEnabled(enabled)
        self.snapshots_combo.setEnabled(enabled)
        self.snapshot_name_input.setEnabled(enabled)
        self.menuBar().setEnabled(enabled)


    def closeEvent(self, event):
        if self.snapshot_worker and self.snapshot_worker.isRunning():
            self.snapshot_worker.request_cancellation()
            self.snapshot_worker.wait(1000) # Give some time to finish/cancel
        if self.comparison_worker and self.comparison_worker.isRunning():
            self.comparison_worker.request_cancellation()
            self.comparison_worker.wait(1000)
        super().closeEvent(event)