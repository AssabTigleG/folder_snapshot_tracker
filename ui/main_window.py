# ui/main_window.py
import os
import shutil
import subprocess
import sys
from pathlib import Path
import csv
from collections import Counter

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QComboBox, QFileDialog, QMessageBox, QTreeWidget,
    QTreeWidgetItem, QLineEdit, QLabel, QProgressBar, QAbstractItemView,
    QMenuBar, QCheckBox, QMenu, QTabWidget, QGroupBox, QFormLayout,
    QSizePolicy
)
from PyQt6.QtCore import Qt, QUrl, QPoint
from PyQt6.QtGui import QAction, QGuiApplication
from typing import Dict, List, Optional, Any

from core.db_manager import DatabaseManager
from core.snapshot_manager import SnapshotWorker
from core.comparison_engine import ComparisonWorker
from utils.config_handler import ConfigManager
from ui.ignore_list_dialog import IgnoreListDialog

class MainWindow(QMainWindow):
    ITEM_TYPE_ROLE = Qt.ItemDataRole.UserRole + 1
    ITEM_DATA_ROLE = Qt.ItemDataRole.UserRole + 2
    TAB_ROOT_PATH_ROLE = Qt.ItemDataRole.UserRole + 3 # For storing root path in tab data

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Folder Snapshot & Change Tracker")
        self.setGeometry(100, 100, 1200, 800)

        self.db_manager = DatabaseManager()
        self.config_manager = ConfigManager()
        self.selected_folders_for_snapshot = []
        self.snapshot_worker: Optional[SnapshotWorker] = None
        self.comparison_worker: Optional[ComparisonWorker] = None
        
        self.current_comparison_root_folders_A: List[str] = []
        self.current_comparison_root_folders_B: List[str] = []
        
        # This will store QTreeWidget instances, keyed by root folder path string
        self.per_root_folder_trees: Dict[str, QTreeWidget] = {}


        self._create_menu_bar()
        self.init_ui()
        self.load_snapshots_into_all_combos()
        self._update_trust_metadata_checkbox_state()

    def _create_menu_bar(self):
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")
        settings_action = QAction("&Settings...", self)
        settings_action.triggered.connect(self.open_settings_dialog)
        file_menu.addAction(settings_action)
        export_action = QAction("&Export Report...", self)
        export_action.triggered.connect(self.export_report_dialog) # Connect to existing
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

    def export_report_dialog(self): # Now needs to consider multiple trees if tabbed
        active_tree = self._get_active_results_tree()
        if not active_tree or active_tree.topLevelItemCount() == 0 :
            # Check if ANY tree has data if no tab is obviously active or empty
            has_any_data = any(tree.topLevelItemCount() > 0 for tree in self.per_root_folder_trees.values())
            if not has_any_data:
                QMessageBox.information(self, "No Data", "There is no comparison data to export.")
                return
            # If active tree is empty but others have data, maybe prompt user? For now, export active or first non-empty.
            if not active_tree or active_tree.topLevelItemCount() == 0:
                for tree in self.per_root_folder_trees.values():
                    if tree.topLevelItemCount() > 0:
                        active_tree = tree
                        # Maybe activate the tab? For now, just use its data.
                        # tab_index = self.results_tab_widget.indexOf(active_tree.parentWidget()) # This is tricky if tree is not direct child
                        # self.results_tab_widget.setCurrentIndex(tab_index)
                        break
            if not active_tree or active_tree.topLevelItemCount() == 0: # Still no data
                 QMessageBox.information(self, "No Data", "No active comparison data to export.")
                 return


        file_path, selected_filter = QFileDialog.getSaveFileName(
            self, "Export Report", "",
            "CSV files (*.csv);;Text files (*.txt);;All Files (*)"
        )
        if not file_path: return

        try:
            report_data = []
            # Iterate through visible items in the active_tree
            for i in range(active_tree.topLevelItemCount()):
                category_item = active_tree.topLevelItem(i)
                if category_item.isHidden(): continue
                category_name = category_item.text(0)
                for j in range(category_item.childCount()):
                    child_item = category_item.child(j)
                    if child_item.isHidden(): continue
                    item_name = child_item.text(1)
                    relative_path = child_item.text(2)
                    details = child_item.text(3)
                    # _get_full_path_for_tree_item needs the item's tree to determine root path
                    full_path_str = str(self._get_full_path_for_tree_item(child_item, active_tree) or "N/A")
                    report_data.append({
                        "Status": category_name, "Name": item_name,
                        "Relative Path": relative_path, "Full Path": full_path_str,
                        "Details": details
                    })
            if not report_data:
                QMessageBox.information(self, "No Visible Data", "No visible items in the current view to export.")
                return

            if selected_filter.startswith("CSV"): self._write_csv_report(file_path, report_data)
            elif selected_filter.startswith("Text"): self._write_txt_report(file_path, report_data)
            else: # Default or "All Files"
                if file_path.lower().endswith(".csv"): self._write_csv_report(file_path, report_data)
                else:
                    if not file_path.lower().endswith(".txt"): file_path += ".txt"
                    self._write_txt_report(file_path, report_data)
            QMessageBox.information(self, "Export Successful", f"Report exported to:\n{file_path}")
            self.status_label.setText(f"Report exported from active tab to {Path(file_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Could not export report: {e}")
            self.status_label.setText(f"Export error: {e}")


    def _write_csv_report(self, file_path: str, data: List[Dict[str, str]]):
        if not data: return
        with open(file_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)

    def _write_txt_report(self, file_path: str, data: List[Dict[str, str]]):
        with open(file_path, 'w', encoding='utf-8') as f:
            for item in data:
                for key, value in item.items():
                    f.write(f"  {key}: {value}\n")
                f.write("-" * 40 + "\n")

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        left_panel_widget = QWidget()
        left_panel_layout = QVBoxLayout(left_panel_widget)

        snapshot_group = QGroupBox("Create New Snapshot")
        # ... (Snapshot creation UI remains largely the same) ...
        snapshot_layout = QVBoxLayout()
        self.folder_list_widget = QListWidget()
        self.folder_list_widget.setFixedHeight(100)
        snapshot_layout.addWidget(QLabel("Folders to Include:"))
        snapshot_layout.addWidget(self.folder_list_widget)
        folder_buttons_layout = QHBoxLayout()
        self.add_folder_button = QPushButton("Add Folder")
        self.add_folder_button.clicked.connect(self.add_folder)
        folder_buttons_layout.addWidget(self.add_folder_button)
        self.remove_folder_button = QPushButton("Remove")
        self.remove_folder_button.clicked.connect(self.remove_selected_folders)
        folder_buttons_layout.addWidget(self.remove_folder_button)
        self.clear_folders_button = QPushButton("Clear All")
        self.clear_folders_button.clicked.connect(self.clear_all_folders)
        folder_buttons_layout.addWidget(self.clear_folders_button)
        snapshot_layout.addLayout(folder_buttons_layout)
        self.snapshot_name_input = QLineEdit()
        self.snapshot_name_input.setPlaceholderText("Optional: Snapshot Name")
        snapshot_layout.addWidget(QLabel("Snapshot Name:"))
        snapshot_layout.addWidget(self.snapshot_name_input)
        self.create_snapshot_button = QPushButton("Create Snapshot")
        self.create_snapshot_button.clicked.connect(self.create_snapshot)
        snapshot_layout.addWidget(self.create_snapshot_button)
        snapshot_group.setLayout(snapshot_layout)
        left_panel_layout.addWidget(snapshot_group)

        self.comparison_tabs = QTabWidget()
        self.comparison_tabs.currentChanged.connect(self._update_active_comparison_roots)
        # ... (Comparison Tabs UI remains largely the same) ...
        live_vs_snap_widget = QWidget()
        live_vs_snap_layout = QVBoxLayout(live_vs_snap_widget)
        live_vs_snap_group = QGroupBox("Compare Live System vs. Snapshot")
        live_vs_snap_group_layout = QVBoxLayout()
        live_vs_snap_group_layout.addWidget(QLabel("Select Snapshot to Compare Against:"))
        self.live_compare_snapshot_combo = QComboBox()
        self.live_compare_snapshot_combo.currentIndexChanged.connect(
            lambda index: self._on_selected_snapshot_changed_for_comparison(index, "A", self.live_compare_snapshot_combo)
        )
        live_vs_snap_group_layout.addWidget(self.live_compare_snapshot_combo)
        self.live_compare_button = QPushButton("Compare Live vs. Selected Snapshot")
        self.live_compare_button.clicked.connect(self.compare_live_vs_snapshot)
        live_vs_snap_group_layout.addWidget(self.live_compare_button)
        live_vs_snap_group.setLayout(live_vs_snap_group_layout)
        live_vs_snap_layout.addWidget(live_vs_snap_group)
        live_vs_snap_layout.addStretch()
        self.comparison_tabs.addTab(live_vs_snap_widget, "Live vs. Snapshot")

        snap_vs_snap_widget = QWidget()
        snap_vs_snap_layout = QVBoxLayout(snap_vs_snap_widget)
        snap_vs_snap_group = QGroupBox("Compare Snapshot A vs. Snapshot B")
        snap_vs_snap_group_layout = QVBoxLayout()
        snap_vs_snap_group_layout.addWidget(QLabel("Select Snapshot A (Older/Reference):"))
        self.snapshot_a_combo = QComboBox()
        self.snapshot_a_combo.currentIndexChanged.connect(
             lambda index: self._on_selected_snapshot_changed_for_comparison(index, "A", self.snapshot_a_combo)
        )
        snap_vs_snap_group_layout.addWidget(self.snapshot_a_combo)
        snap_vs_snap_group_layout.addWidget(QLabel("Select Snapshot B (Newer/To Compare):"))
        self.snapshot_b_combo = QComboBox()
        self.snapshot_b_combo.currentIndexChanged.connect(
             lambda index: self._on_selected_snapshot_changed_for_comparison(index, "B", self.snapshot_b_combo)
        )
        snap_vs_snap_group_layout.addWidget(self.snapshot_b_combo)
        self.snap_compare_button = QPushButton("Compare Snapshot A vs. Snapshot B")
        self.snap_compare_button.clicked.connect(self.compare_snapshot_vs_snapshot)
        snap_vs_snap_group_layout.addWidget(self.snap_compare_button)
        snap_vs_snap_group.setLayout(snap_vs_snap_group_layout)
        snap_vs_snap_layout.addWidget(snap_vs_snap_group)
        snap_vs_snap_layout.addStretch()
        self.comparison_tabs.addTab(snap_vs_snap_widget, "Snapshot A vs. B")
        left_panel_layout.addWidget(self.comparison_tabs)

        common_options_group = QGroupBox("Comparison Options")
        # ... (Common options UI remains largely the same) ...
        common_options_layout = QVBoxLayout()
        self.quick_compare_checkbox = QCheckBox("Quick Compare (Size/Date Only)")
        self.quick_compare_checkbox.stateChanged.connect(self._update_trust_metadata_checkbox_state)
        common_options_layout.addWidget(self.quick_compare_checkbox)
        self.trust_metadata_checkbox = QCheckBox("Trust metadata for unchanged files (Full Compare Only)")
        self.trust_metadata_checkbox.setToolTip(
            "If checked (and Quick Compare is off for Live vs. Snapshot), files with matching LMT and Size\n"
            "to the snapshot will not be re-hashed. Use with caution."
        )
        common_options_layout.addWidget(self.trust_metadata_checkbox)
        common_options_group.setLayout(common_options_layout)
        left_panel_layout.addWidget(common_options_group)
        
        delete_snapshot_group = QGroupBox("Manage Snapshots")
        # ... (Delete snapshot UI remains largely the same) ...
        delete_snapshot_layout = QVBoxLayout()
        delete_snapshot_layout.addWidget(QLabel("Select Snapshot to Delete:"))
        self.delete_snapshot_combo = QComboBox()
        delete_snapshot_layout.addWidget(self.delete_snapshot_combo)
        self.delete_snapshot_button = QPushButton("Delete Selected Snapshot")
        self.delete_snapshot_button.clicked.connect(self.delete_snapshot_handler)
        delete_snapshot_layout.addWidget(self.delete_snapshot_button)
        delete_snapshot_group.setLayout(delete_snapshot_layout)
        left_panel_layout.addWidget(delete_snapshot_group)

        left_panel_layout.addStretch()
        self.progress_bar = QProgressBar() # ... (Progress bar, status, cancel button same) ...
        self.progress_bar.setVisible(False)
        left_panel_layout.addWidget(self.progress_bar)
        self.status_label = QLabel("Idle.")
        self.status_label.setWordWrap(True)
        left_panel_layout.addWidget(self.status_label)
        self.cancel_button = QPushButton("Cancel Operation")
        self.cancel_button.clicked.connect(self.cancel_current_operation)
        self.cancel_button.setVisible(False)
        left_panel_layout.addWidget(self.cancel_button)
        left_panel_widget.setFixedWidth(380)
        main_layout.addWidget(left_panel_widget)

        # --- Right Panel: Results & Summary ---
        right_panel_widget = QWidget()
        right_panel_layout = QVBoxLayout(right_panel_widget)

        filter_search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search results in active tab...")
        self.search_input.textChanged.connect(self.apply_results_filter) # Will apply to active tab's tree
        filter_search_layout.addWidget(self.search_input)
        self.status_filter_combo = QComboBox()
        self.status_filter_combo.addItems(["All Statuses", "Added", "Modified", "Deleted"])
        self.status_filter_combo.currentIndexChanged.connect(self.apply_results_filter) # Will apply to active tab's tree
        filter_search_layout.addWidget(self.status_filter_combo)
        right_panel_layout.addLayout(filter_search_layout)

        # This QTabWidget will hold the per-root-folder result trees
        self.results_tab_widget = QTabWidget()
        self.results_tab_widget.setTabsClosable(False) # Or true if we want to allow closing tabs
        self.results_tab_widget.setMovable(True)
        right_panel_layout.addWidget(QLabel("Comparison Results Details (Per Root Folder):"))
        right_panel_layout.addWidget(self.results_tab_widget, 3) # Main results area

        summary_group = QGroupBox("Global Change Summary") # Summary is global
        # ... (Summary UI remains the same) ...
        summary_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        summary_layout = QFormLayout()
        self.summary_added_label = QLabel("N/A")
        self.summary_modified_label = QLabel("N/A")
        self.summary_deleted_label = QLabel("N/A")
        self.summary_total_files_compared_label = QLabel("N/A")
        self.summary_filetypes_added_label = QLabel("N/A")
        self.summary_filetypes_modified_label = QLabel("N/A")
        self.summary_filetypes_deleted_label = QLabel("N/A")
        self.summary_total_size_added_label = QLabel("N/A")
        self.summary_total_size_deleted_label = QLabel("N/A")

        summary_layout.addRow("Items Added:", self.summary_added_label)
        summary_layout.addRow("Items Modified:", self.summary_modified_label)
        summary_layout.addRow("Items Deleted:", self.summary_deleted_label)
        summary_layout.addRow("Total Items Analyzed:", self.summary_total_files_compared_label)
        summary_layout.addRow("Added File Types (Top 3):", self.summary_filetypes_added_label)
        summary_layout.addRow("Modified File Types (Top 3):", self.summary_filetypes_modified_label)
        summary_layout.addRow("Deleted File Types (Top 3):", self.summary_filetypes_deleted_label)
        summary_layout.addRow("Total Size Added:", self.summary_total_size_added_label)
        summary_layout.addRow("Total Size Deleted:", self.summary_total_size_deleted_label)
        summary_group.setLayout(summary_layout)
        right_panel_layout.addWidget(summary_group, 1)

        main_layout.addWidget(right_panel_widget)

    def _format_size(self, num_bytes: float) -> str: # Same
        if num_bytes is None: return "N/A"
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if abs(num_bytes) < 1024.0:
                return f"{num_bytes:3.1f} {unit}"
            num_bytes /= 1024.0
        return f"{num_bytes:.1f} PB"

    def _update_change_summary(self, results: Dict[str, List[Any]]): # Same
        added_items = results.get('added', [])
        modified_items = results.get('modified', [])
        deleted_items = results.get('deleted', [])
        self.summary_added_label.setText(str(len(added_items)))
        self.summary_modified_label.setText(str(len(modified_items)))
        self.summary_deleted_label.setText(str(len(deleted_items)))
        total_analyzed = len(added_items) + len(deleted_items) + len(modified_items)
        self.summary_total_files_compared_label.setText(str(total_analyzed) + " (changes)")
        def get_top_filetypes(items_list: List[Dict[str, Any]], key_for_item='item_name') -> str:
            if not items_list: return "N/A"
            extensions = Counter()
            for item_data in items_list:
                actual_item = item_data.get('new', item_data) if isinstance(item_data, dict) and 'new' in item_data else item_data
                if actual_item.get('is_file'):
                    name = actual_item.get(key_for_item, "")
                    ext = Path(name).suffix.lower() if Path(name).suffix else ".<no_ext>"
                    extensions[ext] += 1
            if not extensions: return "N/A (no files)"
            return ", ".join([f"{ext} ({count})" for ext, count in extensions.most_common(3)])
        self.summary_filetypes_added_label.setText(get_top_filetypes(added_items))
        self.summary_filetypes_modified_label.setText(get_top_filetypes(modified_items))
        self.summary_filetypes_deleted_label.setText(get_top_filetypes(deleted_items))
        total_size_added = sum(item.get('size', 0) for item in added_items if item.get('is_file') and item.get('size') is not None)
        self.summary_total_size_added_label.setText(self._format_size(total_size_added))
        total_size_deleted = sum(item.get('size', 0) for item in deleted_items if item.get('is_file') and item.get('size') is not None)
        self.summary_total_size_deleted_label.setText(self._format_size(total_size_deleted))
        self.summary_total_files_compared_label.setToolTip("Number of items that were added, deleted, or modified.")

    def _update_trust_metadata_checkbox_state(self, _state=None): # Same
        is_quick_compare_checked = self.quick_compare_checkbox.isChecked()
        if self.snapshot_worker is None and self.comparison_worker is None:
            self.trust_metadata_checkbox.setEnabled(not is_quick_compare_checked)
        if is_quick_compare_checked:
            self.trust_metadata_checkbox.setChecked(False)
            
    def _get_active_results_tree(self) -> Optional[QTreeWidget]:
        if self.results_tab_widget.count() > 0:
            current_tab_content = self.results_tab_widget.currentWidget()
            if isinstance(current_tab_content, QTreeWidget):
                return current_tab_content
            # If the tab content is a container widget, find the QTreeWidget within it.
            # This assumes a simple structure: QTabWidget -> QWidget (tab_page) -> QTreeWidget
            elif isinstance(current_tab_content, QWidget): # Check if it's the container page
                tree = current_tab_content.findChild(QTreeWidget)
                if isinstance(tree, QTreeWidget):
                    return tree
        return None

    def apply_results_filter(self): # Needs to target the active tab's tree
        active_tree = self._get_active_results_tree()
        if not active_tree: return

        search_term = self.search_input.text().lower()
        status_filter = self.status_filter_combo.currentText()
        for i in range(active_tree.topLevelItemCount()):
            cat_item = active_tree.topLevelItem(i)
            if not cat_item: continue
            cat_name = cat_item.text(0)
            cat_visible = False
            if status_filter != "All Statuses" and cat_name != status_filter:
                cat_item.setHidden(True)
                continue
            for j in range(cat_item.childCount()):
                child = cat_item.child(j)
                if not child: continue
                name = child.text(1).lower()
                rel_path = child.text(2).lower()
                matches = (search_term in name) or (search_term in rel_path)
                if matches:
                    child.setHidden(False)
                    cat_visible = True
                else:
                    child.setHidden(True)
            cat_item.setHidden(not cat_visible)
            if cat_visible and (search_term or status_filter != "All Statuses"): # Expand if any filter is active
                cat_item.setExpanded(True)


    def _on_selected_snapshot_changed_for_comparison(self, index: int, snap_ref: str, combo_box: QComboBox): # Same logic
        snapshot_id = combo_box.itemData(index)
        root_folders = []
        if snapshot_id and snapshot_id != -1:
            all_snaps = self.db_manager.get_all_snapshots()
            snap_info = next((s for s in all_snaps if s['id'] == snapshot_id), None)
            if snap_info:
                root_folders = snap_info.get('root_folders', [])
        if combo_box == self.live_compare_snapshot_combo or combo_box == self.snapshot_a_combo:
             self.current_comparison_root_folders_A = root_folders
        elif combo_box == self.snapshot_b_combo:
             self.current_comparison_root_folders_B = root_folders

    def _update_active_comparison_roots(self): # Same logic
        if self.comparison_tabs.currentIndex() == 0:
            live_idx = self.live_compare_snapshot_combo.currentIndex()
            self._on_selected_snapshot_changed_for_comparison(live_idx, "A", self.live_compare_snapshot_combo)
            self.current_comparison_root_folders_B = []
        else:
            a_idx = self.snapshot_a_combo.currentIndex()
            b_idx = self.snapshot_b_combo.currentIndex()
            self._on_selected_snapshot_changed_for_comparison(a_idx, "A", self.snapshot_a_combo)
            self._on_selected_snapshot_changed_for_comparison(b_idx, "B", self.snapshot_b_combo)

    def _get_full_path_for_tree_item(self, item: QTreeWidgetItem, tree_widget: QTreeWidget) -> Optional[Path]:
        item_data_role_value = item.data(0, self.ITEM_DATA_ROLE)
        item_type_role_value = item.data(0, self.ITEM_TYPE_ROLE)

        if item_type_role_value == 'category':
            return None
        
        if not isinstance(item_data_role_value, dict):
            return None

        # Section 1: Determine the root path for the current tab's tree.
        current_tab_root_path_str = getattr(tree_widget, "root_path_str", None)
        if not isinstance(current_tab_root_path_str, str) or not current_tab_root_path_str:
            return None
        
        base_path = Path(current_tab_root_path_str)

        # Section 2: Determine the correct item properties dictionary.
        parent_category_text = item.parent().text(0) if item.parent() else ""
        
        target_item_properties: Optional[Dict[str, Any]] = None
        if parent_category_text == "Modified":
            new_item_dict = item_data_role_value.get('new')
            old_item_dict = item_data_role_value.get('old')
            if isinstance(new_item_dict, dict):
                target_item_properties = new_item_dict
            elif isinstance(old_item_dict, dict): 
                target_item_properties = old_item_dict
        elif parent_category_text in ["Added", "Deleted"]:
            target_item_properties = item_data_role_value
        
        if not isinstance(target_item_properties, dict):
            return None

        # Section 3: Get the relative path string from the item's properties.
        relative_path_str = target_item_properties.get('relative_path')
        
        if not isinstance(relative_path_str, str) or relative_path_str == "": 
            if relative_path_str == ".":
                pass
            else: 
                return None
        
        # Section 4: Construct and return the full path.
        final_path = base_path / relative_path_str
        return final_path


    def show_results_tree_context_menu(self, position: QPoint): # Needs active tree
        active_tree = self._get_active_results_tree()
        if not active_tree: 
            return

        item = active_tree.itemAt(position)
        if not item:
            return
        if item.data(0, self.ITEM_TYPE_ROLE) == 'category':
            return
        
        full_path = self._get_full_path_for_tree_item(item, active_tree)
        # Removed debug print for full_path here, as it was confirmed to be working.

        parent_text = item.parent().text(0) if item.parent() else ""
        item_data_dict = item.data(0, self.ITEM_DATA_ROLE)
        
        # Ensure item_data_dict is a dictionary before proceeding
        if not isinstance(item_data_dict, dict):
            return # Or handle error appropriately

        check_item = item_data_dict
        if parent_text == "Modified": 
            check_item = item_data_dict.get('new', item_data_dict.get('old', {})) # Ensure fallback is a dict
        
        # Ensure check_item is a dictionary
        if not isinstance(check_item, dict):
            return # Or handle error

        item_is_file = check_item.get('is_file', True) # Default to True if key missing
        item_logically_exists = (parent_text == "Added") or (parent_text == "Modified")

        menu = QMenu(self)
        if full_path: # full_path should be a Path object or None
            # The lambda now accepts 'checked' (or '_') to consume the boolean from the signal
            menu.addAction(QAction(f"Copy Full Path: {str(full_path)[:50]}...", self, 
                                   triggered=lambda checked=False, p=full_path: self.copy_item_path(p)))
            
            open_container_path = None
            if item_logically_exists:
                open_container_path = full_path.parent if item_is_file else full_path
            elif parent_text == "Deleted": # Item doesn't exist, so its parent might.
                open_container_path = full_path.parent
            
            if open_container_path: # open_container_path should be a Path object
                is_snap_vs_snap = (self.comparison_tabs.currentIndex() == 1)
                menu.addAction(QAction("Open Containing Folder", self, 
                                       triggered=lambda checked=False, p=open_container_path, snap_context=is_snap_vs_snap: self.open_item_location(p, is_snap_vs_snap_context=snap_context)))
            
            if item_logically_exists and item_is_file: # full_path should be a Path object
                 is_snap_vs_snap = (self.comparison_tabs.currentIndex() == 1)
                 menu.addAction(QAction("Open File", self, 
                                        triggered=lambda checked=False, p=full_path, snap_context=is_snap_vs_snap: self.open_item_location(p, is_snap_vs_snap_context=snap_context)))
        
        if menu.actions(): 
            menu.exec(active_tree.mapToGlobal(position))


    def copy_item_path(self, path: Path): # Same
        if path: QGuiApplication.clipboard().setText(str(path)); self.status_label.setText(f"Path copied: {path}")

    def open_item_location(self, path: Path, is_snap_vs_snap_context: bool = False): # Same, added context
        if not path: 
            self.status_label.setText("Cannot open: Invalid path provided.")
            return
        try:
            effective_path = path
            if is_snap_vs_snap_context and not path.exists(): # For snap vs snap, path might not exist
                 QMessageBox.information(self, "Path Not Found", f"Path from snapshot does not exist on current system:\n{path}")
                 return
            if not path.exists() and path.parent.exists(): effective_path = path.parent
            elif not path.exists(): QMessageBox.warning(self, "Cannot Open", f"Path does not exist: {path}"); return
            if sys.platform == 'win32': os.startfile(str(effective_path))
            elif sys.platform == 'darwin': subprocess.run(['open', str(effective_path)], check=False)
            else: subprocess.run(['xdg-open', str(effective_path)], check=False)
        except Exception as e: QMessageBox.warning(self, "Error Opening", f"Could not open '{path}': {e}")

    def add_folder(self): # Same
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if folder and folder not in self.selected_folders_for_snapshot:
            self.selected_folders_for_snapshot.append(folder)
            self.folder_list_widget.addItem(folder)

    def remove_selected_folders(self): # Same
        for item in self.folder_list_widget.selectedItems():
            self.selected_folders_for_snapshot.remove(item.text())
            self.folder_list_widget.takeItem(self.folder_list_widget.row(item))

    def clear_all_folders(self): # Same
        self.selected_folders_for_snapshot.clear(); self.folder_list_widget.clear()

    def load_snapshots_into_all_combos(self): # Same
        combos = [self.live_compare_snapshot_combo, self.snapshot_a_combo, self.snapshot_b_combo, self.delete_snapshot_combo]
        snapshots = self.db_manager.get_all_snapshots()
        for combo in combos:
            curr_data = combo.currentData()
            combo.clear()
            if not snapshots: combo.addItem("No snapshots available", -1)
            else:
                for s in snapshots: combo.addItem(f"{s['name']} ({s['timestamp']})", s['id'])
                idx = combo.findData(curr_data)
                combo.setCurrentIndex(idx if idx !=-1 else (0 if snapshots else -1) )
        self._update_active_comparison_roots()

    def cancel_current_operation(self): # Same
        self.status_label.setText("Cancellation requested...")
        self.cancel_button.setEnabled(False)
        if self.snapshot_worker and self.snapshot_worker.isRunning(): self.snapshot_worker.request_cancellation()
        elif self.comparison_worker and self.comparison_worker.isRunning(): self.comparison_worker.request_cancellation()
        else: self.status_label.setText("No active operation to cancel."); self.cancel_button.setVisible(False)

    def create_snapshot(self): # Same
        if not self.selected_folders_for_snapshot: QMessageBox.warning(self, "No Folders", "Add folders."); return
        if self.snapshot_worker and self.snapshot_worker.isRunning(): QMessageBox.information(self, "Busy", "Snapshot in progress."); return
        name = self.snapshot_name_input.text().strip()
        self.set_ui_for_operation(True, "snapshot")
        self.status_label.setText("Starting snapshot creation...")
        self.snapshot_worker = SnapshotWorker(list(self.selected_folders_for_snapshot), name, self.config_manager.get_ignore_patterns())
        self.snapshot_worker.progress_updated.connect(self.update_progress)
        self.snapshot_worker.snapshot_complete.connect(self.on_snapshot_complete)
        self.snapshot_worker.snapshot_error.connect(self.on_operation_error)
        self.snapshot_worker.finished.connect(self.on_worker_finished)
        self.snapshot_worker.start()

    def _initiate_comparison(self, mode: str, snap_id_A: int, snap_id_B: Optional[int] = None): # Same logic
        if self.comparison_worker and self.comparison_worker.isRunning(): QMessageBox.information(self, "Busy", "Comparison in progress."); return
        
        self.results_tab_widget.clear() # Clear previous result tabs
        self.per_root_folder_trees.clear() # Clear stored tree widgets
        self._clear_summary_labels()
        self.set_ui_for_operation(True, "comparison")

        is_quick = self.quick_compare_checkbox.isChecked()
        trust_meta = self.trust_metadata_checkbox.isChecked() if not is_quick and mode == "live_vs_snapshot" else False
        status_msg = f"Starting {mode.replace('_', ' ')} comparison"
        if is_quick: status_msg += " (Quick)"
        elif trust_meta: status_msg += " (Full, Trusted Meta)"
        else: status_msg += " (Full)"
        self.status_label.setText(status_msg + "...")
        self.comparison_worker = ComparisonWorker(
            snapshot_id_A=snap_id_A, snapshot_id_B=snap_id_B, comparison_mode=mode,
            ignore_patterns=self.config_manager.get_ignore_patterns(),
            quick_compare=is_quick, trust_metadata_for_unchanged_files=trust_meta
        )
        self.comparison_worker.comparison_progress.connect(self.update_progress)
        self.comparison_worker.comparison_complete.connect(self.on_comparison_complete)
        self.comparison_worker.comparison_error.connect(self.on_operation_error)
        self.comparison_worker.finished.connect(self.on_worker_finished)
        self.comparison_worker.start()

    def compare_live_vs_snapshot(self): # Same logic
        snap_id = self.live_compare_snapshot_combo.currentData()
        if snap_id == -1 or snap_id is None: QMessageBox.warning(self, "No Snapshot", "Select snapshot."); return
        self._update_active_comparison_roots()
        if not self.current_comparison_root_folders_A: QMessageBox.warning(self, "Snapshot Error", "Snapshot has no root folders."); return
        self._initiate_comparison("live_vs_snapshot", snap_id)

    def compare_snapshot_vs_snapshot(self): # Same logic
        sid_A = self.snapshot_a_combo.currentData(); sid_B = self.snapshot_b_combo.currentData()
        if sid_A in [-1, None] or sid_B in [-1, None]: QMessageBox.warning(self, "No Snapshot(s)", "Select Snapshots A & B."); return
        if sid_A == sid_B:
            QMessageBox.information(self, "Same Snapshots", "No changes if compared to itself.");
            self.results_tab_widget.clear(); self.per_root_folder_trees.clear(); self._clear_summary_labels()
            self.status_label.setText("Compared snapshot to itself: No changes.")
            return
        self._update_active_comparison_roots()
        if not self.current_comparison_root_folders_A or not self.current_comparison_root_folders_B:
             QMessageBox.warning(self, "Snapshot Error", "One or both snapshots lack root folder definitions."); return
        self._initiate_comparison("snapshot_vs_snapshot", sid_A, sid_B)

    def delete_snapshot_handler(self): # Same
        sid = self.delete_snapshot_combo.currentData()
        if sid == -1 or sid is None: QMessageBox.warning(self, "No Snapshot", "Select snapshot to delete."); return
        name = self.delete_snapshot_combo.currentText()
        if QMessageBox.question(self, "Confirm Delete", f"Delete: {name}?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.db_manager.delete_snapshot(sid); self.load_snapshots_into_all_combos()
            QMessageBox.information(self, "Deleted", f"Snapshot '{name}' deleted.")
            self.results_tab_widget.clear(); self.per_root_folder_trees.clear(); self._clear_summary_labels()

    def update_progress(self, current, total, message): # Same
        self.progress_bar.setRange(0, total if total > 0 else 0)
        self.progress_bar.setValue(current); self.status_label.setText(message)

    def on_snapshot_complete(self, snapshot_id, name): # Same
        self.status_label.setText(f"Snapshot '{name}' (ID: {snapshot_id}) created.")
        self.load_snapshots_into_all_combos(); self.snapshot_name_input.clear()
        QMessageBox.information(self, "Snapshot Complete", f"Snapshot '{name}' created.")

    def on_comparison_complete(self, results: Dict[str, List]):
        is_quick = self.quick_compare_checkbox.isChecked()
        trust_meta = self.trust_metadata_checkbox.isChecked() and not is_quick and self.comparison_tabs.currentIndex() == 0
        mode_str = "Live vs. Snapshot" if self.comparison_tabs.currentIndex() == 0 else "Snapshot A vs. B"
        status_msg = f"Comparison complete ({mode_str})"
        if is_quick: status_msg += " (Quick)"
        elif trust_meta: status_msg += " (Full, Trusted Meta)"
        else: status_msg += " (Full)"
        self.status_label.setText(status_msg + ".")
        
        self.results_tab_widget.clear() # Clear previous tabs
        self.per_root_folder_trees.clear()
        self._update_change_summary(results) # Global summary

        # Determine the list of root folders for this comparison
        # For live_vs_snapshot, it's current_comparison_root_folders_A
        # For snapshot_vs_snapshot, it's typically A's roots (B should align for meaningful comparison)
        active_root_folders = self.current_comparison_root_folders_A
        if self.comparison_tabs.currentIndex() == 1 and self.current_comparison_root_folders_B: # Snap vs Snap, ensure B is also considered if different (though ideally same)
            # This logic might need refinement if root folder sets can truly differ between A and B in a comparison
            # For now, assume A's roots are the primary reference for tab creation.
            pass


        if not active_root_folders:
            self.status_label.setText(status_msg + " Warning: No root folders defined for this comparison to create tabs.")
            # Optionally display all results in a single "Orphaned Results" tab if no roots
            # For now, just warn and skip tab creation if no roots defined.
            # A fallback single tree could be created here if active_root_folders is empty but results exist
            # tree_widget = self._create_new_results_tree_for_tab()
            # self._populate_tree_with_data(tree_widget, results, -1) # -1 for "all roots"
            # self.results_tab_widget.addTab(tree_widget.parentWidget(), "All Results")
            # self.per_root_folder_trees["all_results_fallback"] = tree_widget
            return


        for root_idx, root_path_str in enumerate(active_root_folders):
            tree_widget = self._create_new_results_tree_for_tab()
            setattr(tree_widget, "root_path_str", root_path_str) # Store root path with the tree

            # Filter results for this specific root_idx
            root_specific_results = {'added': [], 'deleted': [], 'modified': []}
            for cat_name_key in ['added', 'deleted']:
                for item in results.get(cat_name_key, []):
                    if item.get('root_folder_idx') == root_idx:
                        root_specific_results[cat_name_key].append(item)
            for item_mod in results.get('modified', []): # Modified items have 'old' and 'new'
                # Check based on 'new' item's root_folder_idx (or 'old' if 'new' doesn't have it, though it should)
                ref_item = item_mod.get('new', item_mod.get('old', {}))
                if ref_item.get('root_folder_idx') == root_idx:
                    root_specific_results['modified'].append(item_mod)
            
            self._populate_tree_with_data(tree_widget, root_specific_results, is_quick, trust_meta)
            
            tab_label = Path(root_path_str).name if Path(root_path_str).name else root_path_str
            # Wrap the tree in a QWidget to add to tab, standard practice.
            tab_page = QWidget()
            tab_layout = QVBoxLayout(tab_page)
            tab_layout.setContentsMargins(0,0,0,0)
            tab_layout.addWidget(tree_widget)
            self.results_tab_widget.addTab(tab_page, tab_label)
            self.per_root_folder_trees[root_path_str] = tree_widget
            # self.results_tab_widget.setTabData(self.results_tab_widget.count()-1, root_path_str) # Alternative for storing root path

        # Resize columns for the initially active tab
        if self.results_tab_widget.count() > 0:
            initial_tree = self._get_active_results_tree()
            if initial_tree:
                for i in range(initial_tree.columnCount()): initial_tree.resizeColumnToContents(i)
        
        self.apply_results_filter() # Apply to the first tab by default


    def _create_new_results_tree_for_tab(self) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setColumnCount(4)
        tree.setHeaderLabels(["Status", "Name", "Relative Path", "Details"])
        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(self.show_results_tree_context_menu)
        tree.setAlternatingRowColors(True) # ApexUI touch
        tree.setStyleSheet("QTreeWidget { border: 1px solid #444; } QHeaderView::section { background-color: #3a3a3a; color: #eee; padding: 4px; border: 1px solid #555; }")
        return tree

    def _populate_tree_with_data(self, tree_widget: QTreeWidget, data: Dict[str, List], is_quick: bool, trust_meta_active: bool):
        categories = {"Added": data.get('added', []), "Deleted": data.get('deleted', []), "Modified": data.get('modified', [])}
        for cat_name, items in categories.items():
            if not items: continue # Skip empty categories for this root
            cat_item = QTreeWidgetItem(tree_widget, [cat_name, f"({len(items)} items)"])
            cat_item.setData(0, self.ITEM_TYPE_ROLE, 'category')
            for item_data in items:
                item_info = item_data.get('new', item_data) if cat_name == "Modified" or cat_name == "Added" else item_data
                old_item_info = item_data.get('old') if cat_name == "Modified" else None
                details_list = []
                if cat_name == "Modified":
                    if item_info['is_file'] != old_item_info['is_file']: details_list.append("Type changed")
                    elif item_info['is_file']:
                        if abs(item_info.get('lmt',0) - old_item_info.get('lmt',0)) > 1e-6 : details_list.append("LMT changed")
                        if item_info.get('size') != old_item_info.get('size'): details_list.append(f"Size: {self._format_size(old_item_info.get('size'))} -> {self._format_size(item_info.get('size'))}")
                        if is_quick: details_list.append("(Quick Compare)")
                elif item_info.get('is_file'):
                    details_list.append(f"Size: {self._format_size(item_info.get('size'))}")
                    h_status = item_info.get('content_hash')
                    if is_quick and isinstance(h_status, str) and "QUICK_COMPARE" in h_status : details_list.append("(Quick Compare)")
                tree_item = QTreeWidgetItem(cat_item, ["", item_info['item_name'], item_info['relative_path'], "; ".join(d for d in details_list if d) or "N/A"])
                tree_item.setData(0, self.ITEM_DATA_ROLE, item_data)
                tree_item.setData(0, self.ITEM_TYPE_ROLE, 'file' if item_info.get('is_file') else 'folder')
            tree_widget.expandItem(cat_item)
        # No global resize here, do it after all tabs are added for the initially visible one.


    def on_operation_error(self, error_message): # Same
        if "cancel" in error_message.lower(): self.status_label.setText("Operation Cancelled.")
        else: self.status_label.setText(f"Error: {error_message}"); QMessageBox.critical(self, "Operation Error", error_message)

    def on_worker_finished(self): # Same
        self.set_ui_for_operation(False)
        if self.sender() == self.snapshot_worker: self.snapshot_worker = None
        elif self.sender() == self.comparison_worker: self.comparison_worker = None
    
    def _clear_summary_labels(self): # Same
        for attr_name in dir(self):
            if attr_name.startswith("summary_") and isinstance(getattr(self, attr_name), QLabel): getattr(self, attr_name).setText("N/A")

    def set_ui_for_operation(self, is_running: bool, operation_type: Optional[str] = None): # Same
        enabled = not is_running
        self.folder_list_widget.setEnabled(enabled); self.snapshot_name_input.setEnabled(enabled)
        self.add_folder_button.setEnabled(enabled); self.remove_folder_button.setEnabled(enabled)
        self.clear_folders_button.setEnabled(enabled)
        self.create_snapshot_button.setEnabled(enabled if operation_type != "comparison" else False)
        self.live_compare_snapshot_combo.setEnabled(enabled); self.snapshot_a_combo.setEnabled(enabled)
        self.snapshot_b_combo.setEnabled(enabled)
        self.live_compare_button.setEnabled(enabled if operation_type != "snapshot" else False)
        self.snap_compare_button.setEnabled(enabled if operation_type != "snapshot" else False)
        self.comparison_tabs.setEnabled(enabled); self.quick_compare_checkbox.setEnabled(enabled)
        self.trust_metadata_checkbox.setEnabled(enabled and not self.quick_compare_checkbox.isChecked())
        self.delete_snapshot_combo.setEnabled(enabled); self.delete_snapshot_button.setEnabled(enabled)
        self.menuBar().setEnabled(enabled); self.progress_bar.setVisible(is_running)
        self.cancel_button.setVisible(is_running); self.cancel_button.setEnabled(is_running)
        if enabled: self._update_trust_metadata_checkbox_state()

    def closeEvent(self, event): # Same
        if self.snapshot_worker and self.snapshot_worker.isRunning(): self.snapshot_worker.request_cancellation(); self.snapshot_worker.wait(300)
        if self.comparison_worker and self.comparison_worker.isRunning(): self.comparison_worker.request_cancellation(); self.comparison_worker.wait(300)
        super().closeEvent(event)