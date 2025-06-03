# ui/main_window.py
import os 
import shutil 
import subprocess 
from pathlib import Path 
import csv # <<<--- ADD THIS IMPORT

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QComboBox, QFileDialog, QMessageBox, QTreeWidget,
    QTreeWidgetItem, QLineEdit, QLabel, QProgressBar, QAbstractItemView,
    QMenuBar, QCheckBox, QMenu 
)
from PyQt6.QtCore import Qt, QUrl, QPoint 
from PyQt6.QtGui import QAction, QGuiApplication 
from typing import Dict, List, Optional 

from core.db_manager import DatabaseManager
from core.snapshot_manager import SnapshotWorker
from core.comparison_engine import ComparisonWorker
from utils.config_handler import ConfigManager 
from ui.ignore_list_dialog import IgnoreListDialog 

class MainWindow(QMainWindow):
    ITEM_TYPE_ROLE = Qt.ItemDataRole.UserRole + 1 
    ITEM_DATA_ROLE = Qt.ItemDataRole.UserRole + 2 

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Folder Snapshot & Change Tracker")
        self.setGeometry(100, 100, 1000, 700)

        self.db_manager = DatabaseManager()
        self.config_manager = ConfigManager() 
        self.selected_folders_for_snapshot = []
        self.snapshot_worker: Optional[SnapshotWorker] = None
        self.comparison_worker: Optional[ComparisonWorker] = None
        self.current_snapshot_root_folders: List[str] = [] 

        self._create_menu_bar() 
        self.init_ui()
        self.load_snapshots_into_combo()

    def _create_menu_bar(self): 
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")

        settings_action = QAction("&Settings...", self)
        settings_action.triggered.connect(self.open_settings_dialog)
        file_menu.addAction(settings_action)

        export_action = QAction("&Export Report...", self) # <<<--- NEW ACTION
        export_action.triggered.connect(self.export_report_dialog) # <<<--- CONNECT TO NEW SLOT
        file_menu.addAction(export_action)
        
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

    def export_report_dialog(self): # <<<--- NEW SLOT FOR EXPORT
        if self.results_tree.topLevelItemCount() == 0:
            QMessageBox.information(self, "No Data", "There is no comparison data to export.")
            return

        file_path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export Report",
            "", # Default directory (current or last used)
            "CSV files (*.csv);;Text files (*.txt);;All Files (*)"
        )

        if not file_path: # User cancelled
            return

        try:
            report_data = []
            # Iterate through visible items in the tree
            for i in range(self.results_tree.topLevelItemCount()):
                category_item = self.results_tree.topLevelItem(i)
                if category_item.isHidden(): # Skip hidden categories
                    continue
                
                category_name = category_item.text(0) # Status (Added, Modified, Deleted)
                for j in range(category_item.childCount()):
                    child_item = category_item.child(j)
                    if child_item.isHidden(): # Skip hidden items within category
                        continue
                    
                    item_name = child_item.text(1)
                    relative_path = child_item.text(2)
                    details = child_item.text(3)
                    
                    # Try to get full path for more context
                    full_path_obj = self._get_full_path_for_tree_item(child_item)
                    full_path_str = str(full_path_obj) if full_path_obj else "N/A"

                    report_data.append({
                        "Status": category_name,
                        "Name": item_name,
                        "Relative Path": relative_path,
                        "Full Path": full_path_str,
                        "Details": details
                    })
            
            if not report_data:
                QMessageBox.information(self, "No Visible Data", "No visible items in the report to export.")
                return

            if selected_filter.startswith("CSV files"):
                self._write_csv_report(file_path, report_data)
            elif selected_filter.startswith("Text files"):
                self._write_txt_report(file_path, report_data)
            else: # Default or "All Files" - try to infer from extension or default to TXT
                if file_path.lower().endswith(".csv"):
                    self._write_csv_report(file_path, report_data)
                else: # Default to TXT if extension is unknown or .txt
                     if not file_path.lower().endswith(".txt"):
                        file_path += ".txt" # Ensure .txt extension if not provided
                     self._write_txt_report(file_path, report_data)
            
            QMessageBox.information(self, "Export Successful", f"Report exported to:\n{file_path}")
            self.status_label.setText(f"Report exported to {Path(file_path).name}")

        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Could not export report: {e}")
            self.status_label.setText(f"Export error: {e}")

    def _write_csv_report(self, file_path: str, data: List[Dict[str, str]]): # <<<--- NEW HELPER
        if not data: return
        headers = data[0].keys()
        with open(file_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=headers)
            writer.writeheader()
            writer.writerows(data)

    def _write_txt_report(self, file_path: str, data: List[Dict[str, str]]): # <<<--- NEW HELPER
        with open(file_path, 'w', encoding='utf-8') as txtfile:
            for item in data:
                txtfile.write(f"Status: {item['Status']}\n")
                txtfile.write(f"  Name: {item['Name']}\n")
                txtfile.write(f"  Relative Path: {item['Relative Path']}\n")
                txtfile.write(f"  Full Path: {item['Full Path']}\n")
                txtfile.write(f"  Details: {item['Details']}\n")
                txtfile.write("-" * 40 + "\n")


    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        left_panel_layout = QVBoxLayout()
        
        self.folder_list_widget = QListWidget()
        left_panel_layout.addWidget(QLabel("Folders to Snapshot:"))
        left_panel_layout.addWidget(self.folder_list_widget)

        add_folder_button = QPushButton("Add Folder") 
        add_folder_button.clicked.connect(self.add_folder) 
        left_panel_layout.addWidget(add_folder_button)

        remove_folder_button = QPushButton("Remove Selected Folder")
        remove_folder_button.clicked.connect(self.remove_selected_folders)
        left_panel_layout.addWidget(remove_folder_button)

        clear_folders_button = QPushButton("Clear All Folders")
        clear_folders_button.clicked.connect(self.clear_all_folders)
        left_panel_layout.addWidget(clear_folders_button)

        self.snapshot_name_input = QLineEdit()
        self.snapshot_name_input.setPlaceholderText("Optional: Snapshot Name (auto-generated if empty)")
        left_panel_layout.addWidget(QLabel("Snapshot Name:"))
        left_panel_layout.addWidget(self.snapshot_name_input)

        create_snapshot_button = QPushButton("Create Snapshot")
        create_snapshot_button.clicked.connect(self.create_snapshot)
        left_panel_layout.addWidget(create_snapshot_button)
        
        left_panel_layout.addSpacing(20)

        left_panel_layout.addWidget(QLabel("Select Snapshot to Compare/Delete:"))
        self.snapshots_combo = QComboBox()
        self.snapshots_combo.currentIndexChanged.connect(self.on_selected_snapshot_changed) 
        left_panel_layout.addWidget(self.snapshots_combo)

        self.quick_compare_checkbox = QCheckBox("Quick Compare (Size/Date Only)") 
        left_panel_layout.addWidget(self.quick_compare_checkbox) 

        compare_button = QPushButton("Compare with Current State")
        compare_button.clicked.connect(self.compare_snapshot)
        left_panel_layout.addWidget(compare_button)
        
        delete_snapshot_button = QPushButton("Delete Selected Snapshot")
        delete_snapshot_button.clicked.connect(self.delete_snapshot)
        left_panel_layout.addWidget(delete_snapshot_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        left_panel_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True) 
        left_panel_layout.addWidget(self.status_label)

        self.cancel_button = QPushButton("Cancel Operation")
        self.cancel_button.clicked.connect(self.cancel_current_operation)
        self.cancel_button.setVisible(False)
        left_panel_layout.addWidget(self.cancel_button)

        left_panel_layout.addStretch()
        main_layout.addLayout(left_panel_layout, 1) 

        right_panel_layout = QVBoxLayout() 
        
        filter_search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by name or path...")
        self.search_input.textChanged.connect(self.apply_results_filter) 
        filter_search_layout.addWidget(self.search_input)

        self.status_filter_combo = QComboBox()
        self.status_filter_combo.addItems(["All Statuses", "Added", "Modified", "Deleted"])
        self.status_filter_combo.currentIndexChanged.connect(self.apply_results_filter) 
        filter_search_layout.addWidget(self.status_filter_combo)
        right_panel_layout.addLayout(filter_search_layout) 


        self.results_tree = QTreeWidget()
        self.results_tree.setColumnCount(4) 
        self.results_tree.setHeaderLabels(["Status", "Name", "Relative Path", "Details"]) 
        self.results_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu) 
        self.results_tree.customContextMenuRequested.connect(self.show_results_tree_context_menu) 
        right_panel_layout.addWidget(QLabel("Comparison Results:"))
        right_panel_layout.addWidget(self.results_tree)
        main_layout.addLayout(right_panel_layout, 3)

    def apply_results_filter(self): 
        search_term = self.search_input.text().lower()
        status_filter = self.status_filter_combo.currentText()

        for i in range(self.results_tree.topLevelItemCount()):
            category_item = self.results_tree.topLevelItem(i)
            if not category_item: continue

            category_name = category_item.text(0) 
            category_visible = False

            if status_filter != "All Statuses" and category_name != status_filter:
                category_item.setHidden(True)
                continue 

            for j in range(category_item.childCount()):
                child_item = category_item.child(j)
                if not child_item: continue

                item_name = child_item.text(1).lower() 
                item_rel_path = child_item.text(2).lower() 
                
                matches_search = (search_term in item_name) or \
                                 (search_term in item_rel_path)
                
                if matches_search:
                    child_item.setHidden(False)
                    category_visible = True 
                else:
                    child_item.setHidden(True)
            
            category_item.setHidden(not category_visible)
            if category_visible: 
                category_item.setExpanded(True)


    def on_selected_snapshot_changed(self, index: int): 
        snapshot_id = self.snapshots_combo.itemData(index)
        if snapshot_id and snapshot_id != -1:
            all_snapshots = self.db_manager.get_all_snapshots() 
            target_snapshot_info = next((s for s in all_snapshots if s['id'] == snapshot_id), None)
            if target_snapshot_info:
                self.current_snapshot_root_folders = target_snapshot_info.get('root_folders', [])
            else:
                self.current_snapshot_root_folders = []
        else:
            self.current_snapshot_root_folders = []


    def _get_full_path_for_tree_item(self, item: QTreeWidgetItem) -> Optional[Path]: 
        item_data = item.data(0, self.ITEM_DATA_ROLE) 
        item_type = item.data(0, self.ITEM_TYPE_ROLE)

        if not item_data or item_type == 'category':
            return None

        actual_item_info = item_data
        if 'new' in item_data and 'old' in item_data: 
            actual_item_info = item_data['new'] 
            parent_text = item.parent().text(0) if item.parent() else ""
            if parent_text == "Deleted": 
                 actual_item_info = item_data['old']


        root_folder_idx = actual_item_info.get('root_folder_idx')
        relative_path_str = actual_item_info.get('relative_path')

        if root_folder_idx is not None and relative_path_str is not None and \
           self.current_snapshot_root_folders and 0 <= root_folder_idx < len(self.current_snapshot_root_folders):
            base_path = Path(self.current_snapshot_root_folders[root_folder_idx])
            return base_path / relative_path_str
        return None

    def show_results_tree_context_menu(self, position: QPoint): 
        item = self.results_tree.itemAt(position)
        if not item:
            return

        item_type = item.data(0, self.ITEM_TYPE_ROLE)
        if item_type == 'category': 
            return

        full_path = self._get_full_path_for_tree_item(item)
        
        parent_text = item.parent().text(0) if item.parent() else ""
        item_exists_on_disk = (parent_text == "Added" or parent_text == "Modified") and full_path and full_path.exists()
        item_was_file = True 
        item_data_dict = item.data(0, self.ITEM_DATA_ROLE)
        if item_data_dict:
            check_item = item_data_dict
            if 'new' in item_data_dict: check_item = item_data_dict['new'] 
            elif parent_text == "Deleted": check_item = item_data_dict 
            item_was_file = check_item.get('is_file', True)


        menu = QMenu(self)

        if full_path:
            copy_path_action = QAction("Copy Full Path", self)
            copy_path_action.triggered.connect(lambda: self.copy_item_path(full_path))
            menu.addAction(copy_path_action)

            if item_exists_on_disk or parent_text == "Deleted": 
                open_folder_action = QAction("Open Containing Folder", self)
                path_to_open_parent = full_path.parent if (item_exists_on_disk and item_was_file) or parent_text == "Deleted" else full_path
                open_folder_action.triggered.connect(lambda: self.open_item_location(path_to_open_parent))
                menu.addAction(open_folder_action)

            if item_exists_on_disk and item_was_file:
                open_file_action = QAction("Open File", self)
                open_file_action.triggered.connect(lambda: self.open_item_location(full_path))
                menu.addAction(open_file_action)
        
        if menu.actions(): 
            menu.exec(self.results_tree.mapToGlobal(position))

    def copy_item_path(self, path: Path): 
        if path:
            QGuiApplication.clipboard().setText(str(path))
            self.status_label.setText(f"Path copied: {path}")

    def open_item_location(self, path: Path): 
        if not path:
            self.status_label.setText("Cannot open: Path is invalid.")
            return
        try:
            if os.name == 'nt': 
                if path.exists():
                    os.startfile(str(path)) 
                else:
                    parent_dir = path.parent
                    if parent_dir.exists():
                        os.startfile(str(parent_dir))
                    else:
                        QMessageBox.warning(self, "Cannot Open", f"Path or its parent does not exist: {path}")
            elif os.name == 'posix': 
                if path.is_dir() and path.exists():
                    subprocess.run(['xdg-open', str(path)], check=False)
                elif path.exists(): 
                     subprocess.run(['xdg-open', str(path)], check=False)
                else: 
                    parent_dir = path.parent
                    if parent_dir.exists() and parent_dir.is_dir():
                         subprocess.run(['xdg-open', str(parent_dir)], check=False)
                    else:
                        QMessageBox.warning(self, "Cannot Open", f"Path or its parent does not exist: {path}")
            else:
                QMessageBox.information(self, "Not Supported", "Opening files/folders is not directly supported on this OS via this app.")
        except Exception as e:
            QMessageBox.warning(self, "Error Opening", f"Could not open '{path}': {e}")
            self.status_label.setText(f"Error opening: {e}")


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
        current_snapshot_id_data = self.snapshots_combo.currentData()
        self.snapshots_combo.clear()
        snapshots = self.db_manager.get_all_snapshots()
        if not snapshots:
            self.snapshots_combo.addItem("No snapshots available", -1)
            self.current_snapshot_root_folders = [] 
            return
        
        selected_index_to_restore = -1
        for idx, snap in enumerate(snapshots):
            self.snapshots_combo.addItem(f"{snap['name']} ({snap['timestamp']})", snap['id'])
            if snap['id'] == current_snapshot_id_data:
                selected_index_to_restore = idx
        
        if selected_index_to_restore != -1:
            self.snapshots_combo.setCurrentIndex(selected_index_to_restore)
        elif snapshots: 
             self.snapshots_combo.setCurrentIndex(0)
        
        self.on_selected_snapshot_changed(self.snapshots_combo.currentIndex()) 


    def cancel_current_operation(self):
        self.status_label.setText("Cancellation requested...")
        self.cancel_button.setEnabled(False) 
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
        self.progress_bar.setRange(0,0) 
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.status_label.setText("Starting snapshot creation...")
        self.set_ui_for_operation(True)

        self.snapshot_worker = SnapshotWorker(
            list(self.selected_folders_for_snapshot), 
            snapshot_name,
            ignore_patterns 
        )
        self.snapshot_worker.progress_updated.connect(self.update_progress)
        self.snapshot_worker.snapshot_complete.connect(self.on_snapshot_complete)
        self.snapshot_worker.snapshot_error.connect(self.on_operation_error)
        self.snapshot_worker.finished.connect(self.on_worker_finished)
        self.snapshot_worker.start()

    def compare_snapshot(self):
        snapshot_id = self.snapshots_combo.currentData()
        if snapshot_id == -1 or snapshot_id is None: 
            QMessageBox.warning(self, "No Snapshot", "Please select a snapshot to compare.")
            return

        if self.comparison_worker and self.comparison_worker.isRunning():
            QMessageBox.information(self, "Busy", "A comparison operation is already in progress.")
            return
        
        self.on_selected_snapshot_changed(self.snapshots_combo.currentIndex())


        ignore_patterns = self.config_manager.get_ignore_patterns() 
        is_quick_compare = self.quick_compare_checkbox.isChecked() 

        self.results_tree.clear()
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0,0) 
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.status_label.setText(f"Starting comparison{' (Quick Mode)' if is_quick_compare else ''}...")
        self.set_ui_for_operation(True)

        self.comparison_worker = ComparisonWorker(snapshot_id, ignore_patterns, is_quick_compare) 
        self.comparison_worker.comparison_progress.connect(self.update_progress)
        self.comparison_worker.comparison_complete.connect(self.on_comparison_complete)
        self.comparison_worker.comparison_error.connect(self.on_operation_error)
        self.comparison_worker.finished.connect(self.on_worker_finished) 
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
        if total > 0 and current <= total : 
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
        elif total == 0 and current == 0: 
            self.progress_bar.setRange(0,0)
        else: 
            self.progress_bar.setRange(0,0) 
        self.status_label.setText(message)

    def on_snapshot_complete(self, snapshot_id, name):
        self.status_label.setText(f"Snapshot '{name}' created successfully (ID: {snapshot_id}).")
        self.load_snapshots_into_combo()
        self.snapshot_name_input.clear() 
        QMessageBox.information(self, "Snapshot Complete", f"Snapshot '{name}' created.")

    def on_comparison_complete(self, results: Dict[str, List]):
        is_quick_mode = self.quick_compare_checkbox.isChecked() 
        self.status_label.setText(f"Comparison complete{' (Quick Mode)' if is_quick_mode else ''}.")
        self.results_tree.clear()
        
        categories = {
            "Added": results.get('added', []), "Deleted": results.get('deleted', []),
            "Modified": results.get('modified', []) 
        }
        for cat_name, items in categories.items():
            cat_item = QTreeWidgetItem(self.results_tree, [cat_name, f"({len(items)} items)"])
            cat_item.setData(0, self.ITEM_TYPE_ROLE, 'category') 

            for item_data_raw in items:
                item_dict_for_role = item_data_raw

                display_name = ""
                display_rel_path = ""
                display_details_list = []
                is_file_for_role = True 

                if cat_name == "Modified":
                    new_item_info = item_data_raw['new']
                    old_item_info = item_data_raw['old']
                    display_name = new_item_info['item_name']
                    display_rel_path = new_item_info['relative_path']
                    is_file_for_role = new_item_info.get('is_file', True)
                    
                    if new_item_info['is_file'] != old_item_info['is_file']:
                        display_details_list.append(f"Type changed (File<->Folder)")
                    elif new_item_info['is_file']: 
                        if new_item_info.get('size') != old_item_info.get('size'):
                            display_details_list.append(f"Size: {old_item_info.get('size', 'N/A')} -> {new_item_info.get('size', 'N/A')}")
                        if new_item_info.get('lmt') != old_item_info.get('lmt'):
                            display_details_list.append(f"LMT changed")
                        if not is_quick_mode: 
                            oh = old_item_info.get('content_hash', 'N/A')
                            nh = new_item_info.get('content_hash', 'N/A')
                            if oh != nh and not (str(oh).startswith("ERROR") and str(nh).startswith("ERROR")):
                                 display_details_list.append(f"Hash: {str(oh)[:8]}... -> {str(nh)[:8]}...")
                        else: display_details_list.append("(Quick Compare)")
                    else: display_details_list.append("Folder metadata/content changed")
                else: 
                    display_name = item_data_raw['item_name']
                    display_rel_path = item_data_raw['relative_path']
                    is_file_for_role = item_data_raw.get('is_file', True)
                    if item_data_raw['is_file']:
                        display_details_list.append(f"Size: {item_data_raw.get('size', 'N/A')}")
                        if not is_quick_mode or cat_name == "Deleted": 
                            h = item_data_raw.get('content_hash', 'N/A')
                            if h and not str(h).startswith("ERROR") and h != "NOT_HASHED_QUICK_COMPARE" and h != "CANCELLED_HASH_LIVE":
                                display_details_list.append(f"Hash: {str(h)[:8]}...")
                            elif h == "NOT_HASHED_QUICK_COMPARE" and is_quick_mode and cat_name == "Added":
                                display_details_list.append("Hash: (Quick Compare)")
                            elif h: display_details_list.append(f"Hash: {h}")
                        elif is_quick_mode and cat_name == "Added":
                             display_details_list.append("Hash: (Quick Compare)")

                details_str = "; ".join(display_details_list) if display_details_list else "N/A"
                tree_list_item = QTreeWidgetItem(cat_item, ["", display_name, display_rel_path, details_str])
                tree_list_item.setData(0, self.ITEM_DATA_ROLE, item_dict_for_role) 
                tree_list_item.setData(0, self.ITEM_TYPE_ROLE, 'file' if is_file_for_role else 'folder')


            self.results_tree.expandItem(cat_item)
        
        self.apply_results_filter() 
        
        for i in range(self.results_tree.columnCount()):
            self.results_tree.resizeColumnToContents(i)

    def on_operation_error(self, error_message):
        if "cancelled" in error_message.lower() or "cancel requested" in error_message.lower():
             self.status_label.setText(f"Operation Cancelled.")
        else:
            self.status_label.setText(f"Error: {error_message}")
            QMessageBox.critical(self, "Operation Error", error_message)

    def on_worker_finished(self):
        self.progress_bar.setVisible(False)
        self.cancel_button.setVisible(False)
        self.cancel_button.setEnabled(True) 
        self.set_ui_for_operation(False) 

        if self.sender() == self.snapshot_worker:
            self.snapshot_worker = None
        elif self.sender() == self.comparison_worker:
            self.comparison_worker = None


    def set_ui_for_operation(self, is_running: bool):
        enabled = not is_running
        for child_widget in self.findChildren(QWidget):
            if isinstance(child_widget, QPushButton) or \
               isinstance(child_widget, QListWidget) or \
               isinstance(child_widget, QComboBox) or \
               isinstance(child_widget, QLineEdit) or \
               isinstance(child_widget, QCheckBox): 
                if child_widget == self.cancel_button:
                    child_widget.setVisible(is_running) 
                    child_widget.setEnabled(is_running) 
                else:
                    child_widget.setEnabled(enabled)
        self.menuBar().setEnabled(enabled)


    def closeEvent(self, event):
        if self.snapshot_worker and self.snapshot_worker.isRunning():
            self.snapshot_worker.request_cancellation()
            self.snapshot_worker.wait(1000) 
        if self.comparison_worker and self.comparison_worker.isRunning():
            self.comparison_worker.request_cancellation()
            self.comparison_worker.wait(1000)
        super().closeEvent(event)