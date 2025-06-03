# core/snapshot_manager.py
import os
from pathlib import Path
import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import List, Dict, Any, Optional # <<<--- ADD THIS LINE
from PyQt6.QtCore import QThread, pyqtSignal

from utils.hashing_utils import calculate_sha256_for_file
from core.db_manager import DatabaseManager

class SnapshotWorker(QThread):
    progress_updated = pyqtSignal(int, int, str)  # current, total, message
    snapshot_complete = pyqtSignal(int, str) # snapshot_id, name
    snapshot_error = pyqtSignal(str)

    def __init__(self, root_folders: List[str], snapshot_name: Optional[str] = None):
        super().__init__()
        self.root_folders = [Path(p) for p in root_folders]
        self.snapshot_name = snapshot_name or f"Snapshot_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.db_manager = DatabaseManager()

    def run(self):
        try:
            snapshot_items_for_db = []
            files_to_hash_map = {} # Maps absolute_path_str to (root_folder_idx, relative_path_str, item_name)

            total_files_to_scan = 0 # For progress, approximation
            for idx, root_path in enumerate(self.root_folders):
                if root_path.is_dir(): # Only scan if it's a directory
                    for _, _, files in os.walk(root_path):
                        total_files_to_scan += len(files)
            
            self.progress_updated.emit(0, total_files_to_scan, "Starting scan...")
            processed_files_count = 0

            for root_idx, root_folder_path in enumerate(self.root_folders):
                if not root_folder_path.is_dir():
                    self.progress_updated.emit(processed_files_count, total_files_to_scan, f"Warning: Root path {root_folder_path} is not a directory or not accessible. Skipping.")
                    continue

                for dirpath, dirnames, filenames in os.walk(root_folder_path):
                    current_dir_path = Path(dirpath)
                    # Add directories
                    for dirname in dirnames:
                        # Add ignore list logic here if implementing
                        full_path = current_dir_path / dirname
                        relative_path = full_path.relative_to(root_folder_path).as_posix()
                        try:
                            stat_info = full_path.stat(follow_symlinks=False) # Don't follow symlinks for stat
                            snapshot_items_for_db.append({
                                'root_folder_idx': root_idx,
                                'relative_path': relative_path,
                                'item_name': dirname,
                                'is_file': False,
                                'size': None,
                                'lmt': stat_info.st_mtime,
                                'ct': stat_info.st_ctime,
                                'content_hash': None
                            })
                        except OSError as e:
                            self.progress_updated.emit(processed_files_count, total_files_to_scan, f"Error stating dir {full_path}: {e}")


                    # Prepare files for hashing
                    for filename in filenames:
                        # Add ignore list logic here if implementing
                        full_path = current_dir_path / filename
                        if full_path.is_symlink(): # Skip symlinks by default, or handle as per design
                             self.progress_updated.emit(processed_files_count, total_files_to_scan, f"Skipping symlink: {full_path}")
                             continue
                        relative_path = full_path.relative_to(root_folder_path).as_posix()
                        files_to_hash_map[str(full_path)] = (root_idx, relative_path, filename)
            
            # Hash files in parallel
            paths_to_hash_list = list(files_to_hash_map.keys())
            path_hashes = {} # Store resulting hashes {absolute_path_str: hash_str}

            if paths_to_hash_list:
                # Ensure max_workers is reasonable, e.g., os.cpu_count()
                with ProcessPoolExecutor(max_workers=os.cpu_count() or 1) as executor: # Ensure at least 1 worker
                    future_to_path = {
                        executor.submit(calculate_sha256_for_file, path_str): path_str
                        for path_str in paths_to_hash_list
                    }
                    for future in as_completed(future_to_path):
                        original_path_str = future_to_path[future]
                        try:
                            file_hash = future.result()
                            path_hashes[original_path_str] = file_hash
                        except Exception as exc:
                            path_hashes[original_path_str] = "ERROR_HASHING_FUTURE"
                            self.progress_updated.emit(processed_files_count, total_files_to_scan, f"Error hashing {original_path_str}: {exc}")
                        
                        processed_files_count +=1
                        self.progress_updated.emit(processed_files_count, total_files_to_scan, f"Hashed: {Path(original_path_str).name}")

            # Populate file items with hashes
            for abs_path_str, (root_idx, rel_path_str, item_name) in files_to_hash_map.items():
                try:
                    stat_info = Path(abs_path_str).stat(follow_symlinks=False) # Don't follow symlinks
                    snapshot_items_for_db.append({
                        'root_folder_idx': root_idx,
                        'relative_path': rel_path_str,
                        'item_name': item_name,
                        'is_file': True,
                        'size': stat_info.st_size,
                        'lmt': stat_info.st_mtime,
                        'ct': stat_info.st_ctime,
                        'content_hash': path_hashes.get(abs_path_str)
                    })
                except OSError as e:
                     self.progress_updated.emit(processed_files_count, total_files_to_scan, f"Error stating file {abs_path_str}: {e}")


            if not snapshot_items_for_db and not self.root_folders:
                 self.snapshot_error.emit("No folders selected or no items found to snapshot.")
                 return
            if not snapshot_items_for_db and self.root_folders: 
                 self.progress_updated.emit(processed_files_count, total_files_to_scan, "Warning: Selected folders are empty or no accessible items found.")

            snapshot_id = self.db_manager.add_snapshot_record(
                self.snapshot_name,
                datetime.datetime.now().isoformat(),
                [str(p) for p in self.root_folders] 
            )
            if snapshot_items_for_db:
                self.db_manager.batch_insert_snapshot_items(snapshot_id, snapshot_items_for_db)
            
            self.snapshot_complete.emit(snapshot_id, self.snapshot_name)

        except Exception as e:
            self.snapshot_error.emit(f"Snapshot creation failed: {e}")
            import traceback
            traceback.print_exc()