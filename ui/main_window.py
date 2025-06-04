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

        self._create_menu_bar()
        self.init_ui() # This will now correctly assign self.snapshot_a_combo etc.
        self.load_snapshots_into_all_combos()
        self._update_trust_metadata_checkbox_state()

    def _create_menu_bar(self):
        menu_bar = self.menuBar()
        file_menu = menu_bar.addMenu("&File")

        settings_action = QAction("&Settings...", self)
        settings_action.triggered.connect(self.open_settings_dialog)
        file_menu.addAction(settings_action)

        export_action = QAction("&Export Report...", self)
        export_action.triggered.connect(self.export_report_dialog)
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

    def export_report_dialog(self):
        if self.results_tree.topLevelItemCount() == 0:
            QMessageBox.information(self, "No Data", "There is no comparison data to export.")
            return

        file_path, selected_filter = QFileDialog.getSaveFileName(
            self, "Export Report", "",
            "CSV files (*.csv);;Text files (*.txt);;All Files (*)"
        )
        if not file_path: return

        try:
            report_data = []
            for i in range(self.results_tree.topLevelItemCount()):
                category_item = self.results_tree.topLevelItem(i)
                if category_item.isHidden(): continue
                category_name = category_item.text(0)
                for j in range(category_item.childCount()):
                    child_item = category_item.child(j)
                    if child_item.isHidden(): continue
                    item_name = child_item.text(1)
                    relative_path = child_item.text(2)
                    details = child_item.text(3)
                    full_path_str = str(self._get_full_path_for_tree_item(child_item) or "N/A")
                    report_data.append({
                        "Status": category_name, "Name": item_name,
                        "Relative Path": relative_path, "Full Path": full_path_str,
                        "Details": details
                    })
            if not report_data:
                QMessageBox.information(self, "No Visible Data", "No visible items to export.")
                return

            if selected_filter.startswith("CSV"): self._write_csv_report(file_path, report_data)
            elif selected_filter.startswith("Text"): self._write_txt_report(file_path, report_data)
            else:
                if file_path.lower().endswith(".csv"): self._write_csv_report(file_path, report_data)
                else:
                    if not file_path.lower().endswith(".txt"): file_path += ".txt"
                    self._write_txt_report(file_path, report_data)
            QMessageBox.information(self, "Export Successful", f"Report exported to:\n{file_path}")
            self.status_label.setText(f"Report exported to {Path(file_path).name}")
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

        live_vs_snap_widget = QWidget()
        live_vs_snap_layout = QVBoxLayout(live_vs_snap_widget)
        live_vs_snap_group = QGroupBox("Compare Live System vs. Snapshot")
        live_vs_snap_group_layout = QVBoxLayout()
        live_vs_snap_group_layout.addWidget(QLabel("Select Snapshot to Compare Against:"))
        self.live_compare_snapshot_combo = QComboBox() # Assign to self
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
        self.snapshot_a_combo = QComboBox() # Assign to self
        self.snapshot_a_combo.currentIndexChanged.connect(
             lambda index: self._on_selected_snapshot_changed_for_comparison(index, "A", self.snapshot_a_combo)
        )
        snap_vs_snap_group_layout.addWidget(self.snapshot_a_combo)
        snap_vs_snap_group_layout.addWidget(QLabel("Select Snapshot B (Newer/To Compare):"))
        self.snapshot_b_combo = QComboBox() # Assign to self
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
        delete_snapshot_layout = QVBoxLayout()
        delete_snapshot_layout.addWidget(QLabel("Select Snapshot to Delete:"))
        self.delete_snapshot_combo = QComboBox() # Assign to self
        delete_snapshot_layout.addWidget(self.delete_snapshot_combo)
        self.delete_snapshot_button = QPushButton("Delete Selected Snapshot")
        self.delete_snapshot_button.clicked.connect(self.delete_snapshot_handler)
        delete_snapshot_layout.addWidget(self.delete_snapshot_button)
        delete_snapshot_group.setLayout(delete_snapshot_layout)
        left_panel_layout.addWidget(delete_snapshot_group)

        left_panel_layout.addStretch()

        self.progress_bar = QProgressBar()
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

        right_panel_widget = QWidget()
        right_panel_layout = QVBoxLayout(right_panel_widget)

        filter_search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search results by name or path...")
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
        right_panel_layout.addWidget(QLabel("Comparison Results Details:"))
        right_panel_layout.addWidget(self.results_tree, 3)

        summary_group = QGroupBox("Change Summary")
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

    def _format_size(self, num_bytes: float) -> str:
        if num_bytes is None: return "N/A"
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if abs(num_bytes) < 1024.0:
                return f"{num_bytes:3.1f} {unit}"
            num_bytes /= 1024.0
        return f"{num_bytes:.1f} PB"

    def _update_change_summary(self, results: Dict[str, List[Any]]):
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

    def _update_trust_metadata_checkbox_state(self, _state=None):
        is_quick_compare_checked = self.quick_compare_checkbox.isChecked()
        if self.snapshot_worker is None and self.comparison_worker is None:
            self.trust_metadata_checkbox.setEnabled(not is_quick_compare_checked)
        if is_quick_compare_checked:
            self.trust_metadata_checkbox.setChecked(False)

    def apply_results_filter(self):
        search_term = self.search_input.text().lower()
        status_filter = self.status_filter_combo.currentText()
        for i in range(self.results_tree.topLevelItemCount()):
            cat_item = self.results_tree.topLevelItem(i)
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
            if cat_visible and search_term: cat_item.setExpanded(True)

    def _on_selected_snapshot_changed_for_comparison(self, index: int, snap_ref: str, combo_box: QComboBox):
        snapshot_id = combo_box.itemData(index)
        root_folders = []
        if snapshot_id and snapshot_id != -1:
            all_snaps = self.db_manager.get_all_snapshots() # Consider optimizing if this becomes slow
            snap_info = next((s for s in all_snaps if s['id'] == snapshot_id), None)
            if snap_info:
                root_folders = snap_info.get('root_folders', [])
        
        if combo_box == self.live_compare_snapshot_combo or combo_box == self.snapshot_a_combo:
             self.current_comparison_root_folders_A = root_folders
        elif combo_box == self.snapshot_b_combo:
             self.current_comparison_root_folders_B = root_folders

    def _update_active_comparison_roots(self):
        if self.comparison_tabs.currentIndex() == 0: # Live vs Snapshot
            live_idx = self.live_compare_snapshot_combo.currentIndex()
            self._on_selected_snapshot_changed_for_comparison(live_idx, "A", self.live_compare_snapshot_combo)
            self.current_comparison_root_folders_B = [] # Not used in this mode
        else: # Snapshot A vs. B
            a_idx = self.snapshot_a_combo.currentIndex()
            b_idx = self.snapshot_b_combo.currentIndex()
            self._on_selected_snapshot_changed_for_comparison(a_idx, "A", self.snapshot_a_combo)
            self._on_selected_snapshot_changed_for_comparison(b_idx, "B", self.snapshot_b_combo)

    def _get_full_path_for_tree_item(self, item: QTreeWidgetItem) -> Optional[Path]:
        item_data = item.data(0, self.ITEM_DATA_ROLE)
        if not item_data or item.data(0, self.ITEM_TYPE_ROLE) == 'category': return None

        parent_text = item.parent().text(0) if item.parent() else ""
        actual_item_info = item_data
        
        current_tab_index = self.comparison_tabs.currentIndex()
        base_paths_list = []

        if current_tab_index == 0: # Live vs. Snapshot
            base_paths_list = self.current_comparison_root_folders_A
            if parent_text == "Added": # 'new' item is from live system
                actual_item_info = item_data
            elif parent_text == "Deleted": # 'old' item from Snapshot A
                actual_item_info = item_data
            elif parent_text == "Modified":
                actual_item_info = item_data.get('new', item_data.get('old'))
        
        elif current_tab_index == 1: # Snapshot A vs. B
            if parent_text == "Added": # 'new' item is from Snapshot B
                actual_item_info = item_data
                base_paths_list = self.current_comparison_root_folders_B
            elif parent_text == "Deleted": # 'old' item from Snapshot A
                actual_item_info = item_data
                base_paths_list = self.current_comparison_root_folders_A
            elif parent_text == "Modified":
                actual_item_info = item_data.get('new', item_data.get('old'))
                base_paths_list = self.current_comparison_root_folders_B # 'new' is from B
        
        if not actual_item_info: return None
        root_folder_idx = actual_item_info.get('root_folder_idx')
        relative_path_str = actual_item_info.get('relative_path')

        if root_folder_idx is not None and relative_path_str is not None and \
           base_paths_list and 0 <= root_folder_idx < len(base_paths_list):
            return Path(base_paths_list[root_folder_idx]) / relative_path_str
        return None

    def show_results_tree_context_menu(self, position: QPoint):
        item = self.results_tree.itemAt(position)
        if not item or item.data(0, self.ITEM_TYPE_ROLE) == 'category': return

        full_path = self._get_full_path_for_tree_item(item)
        parent_text = item.parent().text(0) if item.parent() else ""
        
        item_data_dict = item.data(0, self.ITEM_DATA_ROLE)
        check_item = item_data_dict
        if parent_text == "Modified": check_item = item_data_dict.get('new', item_data_dict.get('old'))
        
        item_is_file = check_item.get('is_file', True)
        
        item_logically_exists = False
        if self.comparison_tabs.currentIndex() == 0: # Live vs. Snapshot
            item_logically_exists = (parent_text == "Added" or parent_text == "Modified")
        else: # Snapshot A vs. B
            # For Snap vs Snap, "existence" refers to being in Snap B (for Added/Newer version of Modified)
            item_logically_exists = (parent_text == "Added") or (parent_text == "Modified")


        menu = QMenu(self)
        if full_path:
            menu.addAction(QAction(f"Copy Full Path: {str(full_path)[:50]}...", self, triggered=lambda: self.copy_item_path(full_path)))
            
            open_container_path = None
            # For Live compare: if added/modified and is file, parent. If dir, itself. If deleted, parent of where it was.
            # For SnapA-SnapB: if in B (added/new-modified) and file, parent of B. If dir, B dir. If deleted (in A not B), parent of A.
            if item_logically_exists: # Item is 'new' or 'added' (from live or SnapB)
                open_container_path = full_path.parent if item_is_file else full_path
            elif parent_text == "Deleted": # Item was 'old' (from SnapA)
                open_container_path = full_path.parent

            if open_container_path:
                # Note: Opening location for SnapA vs SnapB might not make sense if snapshots are from different machines/paths
                # This assumes paths are still relevant on the current machine if possible.
                menu.addAction(QAction("Open Containing Folder", self, triggered=lambda p=open_container_path: self.open_item_location(p)))

            if item_logically_exists and item_is_file:
                 menu.addAction(QAction("Open File", self, triggered=lambda p=full_path: self.open_item_location(p)))
        
        if menu.actions(): menu.exec(self.results_tree.mapToGlobal(position))

    def copy_item_path(self, path: Path):
        if path:
            QGuiApplication.clipboard().setText(str(path))
            self.status_label.setText(f"Path copied: {path}")

    def open_item_location(self, path: Path):
        if not path:
            self.status_label.setText("Cannot open: Path is invalid.")
            return
        try:
            effective_path = path
            # For SnapA vs SnapB, the path might not exist on the current system.
            # We only attempt to open if it's a live comparison result or if path physically exists.
            is_live_compare = self.comparison_tabs.currentIndex() == 0
            if not is_live_compare and not path.exists():
                 QMessageBox.information(self, "Path Not Found", f"Path from snapshot does not exist on current system:\n{path}")
                 return

            if not path.exists() and path.parent.exists():
                effective_path = path.parent
            elif not path.exists():
                 QMessageBox.warning(self, "Cannot Open", f"Path does not exist: {path}")
                 return

            if sys.platform == 'win32': os.startfile(str(effective_path))
            elif sys.platform == 'darwin': subprocess.run(['open', str(effective_path)], check=False)
            else: subprocess.run(['xdg-open', str(effective_path)], check=False)
        except Exception as e:
            QMessageBox.warning(self, "Error Opening", f"Could not open '{path}': {e}")

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if folder and folder not in self.selected_folders_for_snapshot:
            self.selected_folders_for_snapshot.append(folder)
            self.folder_list_widget.addItem(folder)

    def remove_selected_folders(self):
        for item in self.folder_list_widget.selectedItems():
            self.selected_folders_for_snapshot.remove(item.text())
            self.folder_list_widget.takeItem(self.folder_list_widget.row(item))

    def clear_all_folders(self):
        self.selected_folders_for_snapshot.clear()
        self.folder_list_widget.clear()

    def load_snapshots_into_all_combos(self):
        combos_to_update = [
            self.live_compare_snapshot_combo,
            self.snapshot_a_combo,
            self.snapshot_b_combo,
            self.delete_snapshot_combo
        ]
        snapshots = self.db_manager.get_all_snapshots()
        for combo in combos_to_update:
            current_data = combo.currentData()
            combo.clear()
            if not snapshots:
                combo.addItem("No snapshots available", -1)
            else:
                for snap in snapshots:
                    combo.addItem(f"{snap['name']} ({snap['timestamp']})", snap['id'])
                idx_to_restore = combo.findData(current_data)
                if idx_to_restore != -1: combo.setCurrentIndex(idx_to_restore)
                elif snapshots: combo.setCurrentIndex(0)
        self._update_active_comparison_roots()

    def cancel_current_operation(self):
        self.status_label.setText("Cancellation requested...")
        self.cancel_button.setEnabled(False)
        if self.snapshot_worker and self.snapshot_worker.isRunning(): self.snapshot_worker.request_cancellation()
        elif self.comparison_worker and self.comparison_worker.isRunning(): self.comparison_worker.request_cancellation()
        else:
            self.status_label.setText("No active operation to cancel.")
            self.cancel_button.setVisible(False)

    def create_snapshot(self):
        if not self.selected_folders_for_snapshot:
            QMessageBox.warning(self, "No Folders", "Please add folders for the snapshot.")
            return
        if self.snapshot_worker and self.snapshot_worker.isRunning():
            QMessageBox.information(self, "Busy", "Snapshot operation in progress.")
            return
        name = self.snapshot_name_input.text().strip()
        self.set_ui_for_operation(True, "snapshot")
        self.status_label.setText("Starting snapshot creation...")
        self.snapshot_worker = SnapshotWorker(list(self.selected_folders_for_snapshot), name, self.config_manager.get_ignore_patterns())
        self.snapshot_worker.progress_updated.connect(self.update_progress)
        self.snapshot_worker.snapshot_complete.connect(self.on_snapshot_complete)
        self.snapshot_worker.snapshot_error.connect(self.on_operation_error)
        self.snapshot_worker.finished.connect(self.on_worker_finished)
        self.snapshot_worker.start()

    def _initiate_comparison(self, mode: str, snap_id_A: int, snap_id_B: Optional[int] = None):
        if self.comparison_worker and self.comparison_worker.isRunning():
            QMessageBox.information(self, "Busy", "A comparison operation is already in progress.")
            return

        self.results_tree.clear()
        self._clear_summary_labels()
        self.set_ui_for_operation(True, "comparison")

        is_quick = self.quick_compare_checkbox.isChecked()
        trust_meta = self.trust_metadata_checkbox.isChecked() if not is_quick and mode == "live_vs_snapshot" else False
        
        status_msg = f"Starting {mode.replace('_', ' ')} comparison"
        if is_quick: status_msg += " (Quick Mode)"
        elif trust_meta: status_msg += " (Full Mode, Trusting Metadata)"
        else: status_msg += " (Full Mode)"
        self.status_label.setText(status_msg + "...")
        
        self.comparison_worker = ComparisonWorker(
            snapshot_id_A=snap_id_A, snapshot_id_B=snap_id_B,
            comparison_mode=mode,
            ignore_patterns=self.config_manager.get_ignore_patterns(),
            quick_compare=is_quick,
            trust_metadata_for_unchanged_files=trust_meta
        )
        self.comparison_worker.comparison_progress.connect(self.update_progress)
        self.comparison_worker.comparison_complete.connect(self.on_comparison_complete)
        self.comparison_worker.comparison_error.connect(self.on_operation_error)
        self.comparison_worker.finished.connect(self.on_worker_finished)
        self.comparison_worker.start()

    def compare_live_vs_snapshot(self):
        snap_id = self.live_compare_snapshot_combo.currentData()
        if snap_id == -1 or snap_id is None:
            QMessageBox.warning(self, "No Snapshot", "Please select a snapshot.")
            return
        self._update_active_comparison_roots()
        if not self.current_comparison_root_folders_A:
            QMessageBox.warning(self, "Snapshot Error", "Selected snapshot has no root folders defined.")
            return
        self._initiate_comparison("live_vs_snapshot", snap_id)

    def compare_snapshot_vs_snapshot(self):
        snap_id_A = self.snapshot_a_combo.currentData()
        snap_id_B = self.snapshot_b_combo.currentData()
        if snap_id_A == -1 or snap_id_A is None or snap_id_B == -1 or snap_id_B is None:
            QMessageBox.warning(self, "No Snapshot(s)", "Please select both Snapshot A and Snapshot B.")
            return
        if snap_id_A == snap_id_B:
            QMessageBox.information(self, "Same Snapshots", "Comparing a snapshot to itself will show no changes.")
            self.results_tree.clear(); self._clear_summary_labels()
            self.status_label.setText("Compared snapshot to itself: No changes.")
            return
        self._update_active_comparison_roots()
        if not self.current_comparison_root_folders_A or not self.current_comparison_root_folders_B:
             QMessageBox.warning(self, "Snapshot Error", "One or both selected snapshots have no root folders defined.")
             return
        self._initiate_comparison("snapshot_vs_snapshot", snap_id_A, snap_id_B)

    def delete_snapshot_handler(self):
        snapshot_id = self.delete_snapshot_combo.currentData()
        if snapshot_id == -1 or snapshot_id is None:
            QMessageBox.warning(self, "No Snapshot", "Please select a snapshot to delete.")
            return
        snap_name = self.delete_snapshot_combo.currentText()
        reply = QMessageBox.question(self, "Confirm Delete",
                                     f"Are you sure you want to delete: {snap_name}?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.db_manager.delete_snapshot(snapshot_id)
            self.load_snapshots_into_all_combos()
            QMessageBox.information(self, "Deleted", f"Snapshot '{snap_name}' deleted.")
            self.results_tree.clear(); self._clear_summary_labels()

    def update_progress(self, current, total, message):
        self.progress_bar.setRange(0, total if total > 0 else 0)
        self.progress_bar.setValue(current)
        self.status_label.setText(message)

    def on_snapshot_complete(self, snapshot_id, name):
        self.status_label.setText(f"Snapshot '{name}' (ID: {snapshot_id}) created.")
        self.load_snapshots_into_all_combos()
        self.snapshot_name_input.clear()
        QMessageBox.information(self, "Snapshot Complete", f"Snapshot '{name}' created.")

    def on_comparison_complete(self, results: Dict[str, List]):
        is_quick = self.quick_compare_checkbox.isChecked()
        trust_meta_active = self.trust_metadata_checkbox.isChecked() and not is_quick and self.comparison_tabs.currentIndex() == 0
        mode_str = "Live vs. Snapshot" if self.comparison_tabs.currentIndex() == 0 else "Snapshot A vs. B"
        status_msg = f"Comparison complete ({mode_str})"
        if is_quick: status_msg += " (Quick)"
        elif trust_meta_active: status_msg += " (Full, Trusted Meta)"
        else: status_msg += " (Full)"
        self.status_label.setText(status_msg + ".")
        self.results_tree.clear()
        self._update_change_summary(results)

        categories = {"Added": results.get('added', []), "Deleted": results.get('deleted', []), "Modified": results.get('modified', [])}
        for cat_name, items in categories.items():
            cat_item = QTreeWidgetItem(self.results_tree, [cat_name, f"({len(items)} items)"])
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
                        oh, nh = old_item_info.get('content_hash'), item_info.get('content_hash')
                        if not is_quick and oh != nh and not (str(oh).startswith("ERROR") or str(nh).startswith("ERROR")):
                            details_list.append(f"Hash: {str(oh)[:8]}... -> {str(nh)[:8]}...")
                        elif is_quick: details_list.append("(Quick Compare)")
                elif item_info.get('is_file'):
                    details_list.append(f"Size: {self._format_size(item_info.get('size'))}")
                    h = item_info.get('content_hash')
                    if not is_quick and h and not str(h).startswith("ERROR") and "QUICK_COMPARE" not in str(h) and "TRUSTED_HASH_MISSING" not in str(h): details_list.append(f"Hash: {str(h)[:8]}...")
                    elif is_quick and "QUICK_COMPARE" in str(h) : details_list.append("(Quick Compare)")
                tree_item = QTreeWidgetItem(cat_item, ["", item_info['item_name'], item_info['relative_path'], "; ".join(d for d in details_list if d) or "N/A"])
                tree_item.setData(0, self.ITEM_DATA_ROLE, item_data)
                tree_item.setData(0, self.ITEM_TYPE_ROLE, 'file' if item_info.get('is_file') else 'folder')
            self.results_tree.expandItem(cat_item)
        self.apply_results_filter()
        for i in range(self.results_tree.columnCount()): self.results_tree.resizeColumnToContents(i)

    def on_operation_error(self, error_message):
        if "cancel" in error_message.lower(): self.status_label.setText("Operation Cancelled.")
        else:
            self.status_label.setText(f"Error: {error_message}")
            QMessageBox.critical(self, "Operation Error", error_message)

    def on_worker_finished(self):
        self.set_ui_for_operation(False)
        if self.sender() == self.snapshot_worker: self.snapshot_worker = None
        elif self.sender() == self.comparison_worker: self.comparison_worker = None
    
    def _clear_summary_labels(self):
        for attr_name in dir(self):
            if attr_name.startswith("summary_") and isinstance(getattr(self, attr_name), QLabel):
                getattr(self, attr_name).setText("N/A")

    def set_ui_for_operation(self, is_running: bool, operation_type: Optional[str] = None):
        enabled = not is_running
        self.folder_list_widget.setEnabled(enabled)
        self.snapshot_name_input.setEnabled(enabled)
        self.add_folder_button.setEnabled(enabled)
        self.remove_folder_button.setEnabled(enabled)
        self.clear_folders_button.setEnabled(enabled)
        self.create_snapshot_button.setEnabled(enabled if operation_type != "comparison" else False)

        self.live_compare_snapshot_combo.setEnabled(enabled)
        self.snapshot_a_combo.setEnabled(enabled)
        self.snapshot_b_combo.setEnabled(enabled)
        self.live_compare_button.setEnabled(enabled if operation_type != "snapshot" else False)
        self.snap_compare_button.setEnabled(enabled if operation_type != "snapshot" else False)
        self.comparison_tabs.setEnabled(enabled)

        self.quick_compare_checkbox.setEnabled(enabled)
        self.trust_metadata_checkbox.setEnabled(enabled and not self.quick_compare_checkbox.isChecked())
        self.delete_snapshot_combo.setEnabled(enabled)
        self.delete_snapshot_button.setEnabled(enabled)

        self.menuBar().setEnabled(enabled)
        self.progress_bar.setVisible(is_running)
        self.cancel_button.setVisible(is_running)
        self.cancel_button.setEnabled(is_running)
        if enabled: self._update_trust_metadata_checkbox_state()

    def closeEvent(self, event):
        if self.snapshot_worker and self.snapshot_worker.isRunning():
            self.snapshot_worker.request_cancellation()
            self.snapshot_worker.wait(300)
        if self.comparison_worker and self.comparison_worker.isRunning():
            self.comparison_worker.request_cancellation()
            self.comparison_worker.wait(300)
        super().closeEvent(event)