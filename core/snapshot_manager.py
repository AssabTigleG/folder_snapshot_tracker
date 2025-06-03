# core/snapshot_manager.py
import os
from pathlib import Path
import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import List, Dict, Any, Optional 
from PyQt6.QtCore import QThread, pyqtSignal
import fnmatch

from utils.hashing_utils import calculate_sha256_for_file
from core.db_manager import DatabaseManager

class SnapshotWorker(QThread):
    progress_updated = pyqtSignal(int, int, str)  
    snapshot_complete = pyqtSignal(int, str) 
    snapshot_error = pyqtSignal(str)

    def __init__(self, root_folders: List[str], snapshot_name: Optional[str] = None, ignore_patterns: Optional[List[str]] = None):
        super().__init__()
        self.root_folders = [Path(p) for p in root_folders]
        self.snapshot_name = snapshot_name or f"Snapshot_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.db_manager = DatabaseManager()
        self.ignore_patterns = ignore_patterns or []
    
    def _is_ignored(self, name: str, path: Path) -> bool:
        # Check against name first (e.g. ".git", "node_modules")
        for pattern in self.ignore_patterns:
            if fnmatch.fnmatch(name, pattern):
                return True
        # Check against full relative path if pattern ends with / (indicating directory pattern)
        # This is a simple heuristic; more robust pattern matching might be needed for complex cases
        # For now, we mostly rely on name matching for dirs and files.
        # A pattern like "some_dir/" will match a directory named "some_dir".
        # A pattern like "*.log" will match any file ending in .log.
        return False


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

                # Use a list for dirnames to allow modification for skipping
                for dirpath, dirnames_orig, filenames in os.walk(root_folder_path, topdown=True):
                    dirnames = list(dirnames_orig) # Make a mutable copy

                    # Filter directories to ignore
                    # Iterate in reverse to safely remove items from dirnames
                    for i in range(len(dirnames) - 1, -1, -1):
                        dirname = dirnames[i]
                        dir_full_path = Path(dirpath) / dirname
                        if self._is_ignored(dirname, dir_full_path.relative_to(root_folder_path)):
                            self.progress_updated.emit(processed_files_count, total_files_to_scan, f"Ignoring dir: {dirname}")
                            del dirnames[i] # Don't descend into this directory
                            del dirnames_orig[i] # Also modify the original list os.walk uses

                    current_dir_path = Path(dirpath)
                    # Add (non-ignored) directories found at this level
                    for dirname in dirnames: # These are the ones not filtered out
                        full_path = current_dir_path / dirname
                        relative_path = full_path.relative_to(root_folder_path).as_posix()
                        try:
                            stat_info = full_path.stat(follow_symlinks=False)
                            snapshot_items_for_db.append({
                                'root_folder_idx': root_idx,
                                'relative_path': relative_path,
                                'item_name': dirname,
                                'is_file': False, 'size': None, 'lmt': stat_info.st_mtime,
                                'ct': stat_info.st_ctime, 'content_hash': None
                            })
                        except OSError as e:
                            self.progress_updated.emit(processed_files_count, total_files_to_scan, f"Error stating dir {full_path}: {e}")

                    # Prepare (non-ignored) files for hashing
                    for filename in filenames:
                        full_path = current_dir_path / filename
                        if self._is_ignored(filename, full_path.relative_to(root_folder_path)):
                            self.progress_updated.emit(processed_files_count, total_files_to_scan, f"Ignoring file: {filename}")
                            total_files_to_scan -=1 # Adjust total if we skip before counting for progress
                            if total_files_to_scan < 0: total_files_to_scan = 0
                            continue # Skip this file

                        if full_path.is_symlink():
                             self.progress_updated.emit(processed_files_count, total_files_to_scan, f"Skipping symlink: {full_path}")
                             total_files_to_scan -=1
                             if total_files_to_scan < 0: total_files_to_scan = 0
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