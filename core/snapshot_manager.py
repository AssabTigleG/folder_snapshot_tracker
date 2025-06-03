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
    progress_updated = pyqtSignal(int, int, str)  # current, total, message
    snapshot_complete = pyqtSignal(int, str) # snapshot_id, name
    snapshot_error = pyqtSignal(str)

    def __init__(self, root_folders: List[str], 
                 snapshot_name: Optional[str] = None, 
                 ignore_patterns: Optional[List[str]] = None):
        super().__init__()
        self.root_folders = [Path(p) for p in root_folders]
        self.snapshot_name = snapshot_name or f"Snapshot_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.db_manager = DatabaseManager()
        self.ignore_patterns = ignore_patterns or []
        self.current_root_for_item: Optional[Path] = None # For relative path context in _is_ignored if needed

    def _is_ignored(self, item_name: str, item_full_path_obj: Path, is_dir: bool) -> bool:
        """
        Checks if an item should be ignored based on its name, path, and type.
        item_name: The direct name of the file or directory.
        item_full_path_obj: The full Path object of the item.
        is_dir: Boolean, True if the item is a directory.
        """
        for pattern in self.ignore_patterns:
            pattern_lower = pattern.lower()
            item_name_lower = item_name.lower()

            # 1. Direct name match (case-insensitive)
            if fnmatch.fnmatchcase(item_name_lower, pattern_lower):
                return True

            # 2. Glob pattern matching for files (e.g., "*.log", "temp_*.txt")
            if not is_dir and ('*' in pattern or '?' in pattern or '[' in pattern): # More robust glob check
                if fnmatch.fnmatchcase(item_name_lower, pattern_lower):
                    return True
            
            # 3. Handling directory patterns that might have a trailing slash in user input
            if is_dir and pattern.endswith('/'):
                if fnmatch.fnmatchcase(item_name_lower, pattern_lower[:-1]): # Match against name without trailing slash
                    return True
            
            # 4. (Future Enhancement Idea) Match against relative path for more complex patterns
            #    Example: "build/outputs/*.apk"
            #    Requires self.current_root_for_item to be set correctly before calling.
            #    if self.current_root_for_item:
            #        try:
            #            relative_item_path_str = item_full_path_obj.relative_to(self.current_root_for_item).as_posix()
            #            if fnmatch.fnmatchcase(relative_item_path_str.lower(), pattern_lower):
            #                return True
            #        except ValueError: # Not relative, shouldn't happen if current_root_for_item is correct
            #            pass
        return False

    def run(self):
        try:
            snapshot_items_for_db = []
            files_to_hash_map = {} 

            total_files_to_scan_estimate = 0 
            for root_path in self.root_folders:
                if root_path.is_dir(): 
                    try:
                        for _, _, files in os.walk(root_path):
                            total_files_to_scan_estimate += len(files)
                    except OSError: # Handle cases where root path might become inaccessible
                        pass
            
            self.progress_updated.emit(0, total_files_to_scan_estimate, "Starting scan...")
            processed_items_count = 0 # Counts both files and dirs processed for progress

            for root_idx, root_folder_path in enumerate(self.root_folders):
                self.current_root_for_item = root_folder_path # Set context for _is_ignored (if using relative path matching)
                if not root_folder_path.is_dir():
                    self.progress_updated.emit(processed_items_count, total_files_to_scan_estimate, f"Warning: Root path {root_folder_path} is not a directory or not accessible. Skipping.")
                    continue

                # Use a list for dirnames to allow modification for skipping
                for dirpath, dirnames_orig, filenames in os.walk(root_folder_path, topdown=True):
                    dirnames_mutable = list(dirnames_orig) # Make a mutable copy

                    # Filter directories to ignore
                    for i in range(len(dirnames_mutable) - 1, -1, -1):
                        dirname = dirnames_mutable[i]
                        dir_full_path_obj = Path(dirpath) / dirname
                        if self._is_ignored(dirname, dir_full_path_obj, True):
                            self.progress_updated.emit(processed_items_count, total_files_to_scan_estimate, f"Ignoring dir: {dirname}")
                            del dirnames_mutable[i] 
                            if i < len(dirnames_orig): # Ensure index is valid for original list
                                del dirnames_orig[i] # Also modify the original list os.walk uses for descent
                        processed_items_count +=1


                    current_dir_path = Path(dirpath)
                    # Add (non-ignored) directories found at this level
                    for dirname in dirnames_mutable: 
                        full_path_obj = current_dir_path / dirname
                        relative_path_str = full_path_obj.relative_to(root_folder_path).as_posix()
                        try:
                            stat_info = full_path_obj.stat(follow_symlinks=False)
                            snapshot_items_for_db.append({
                                'root_folder_idx': root_idx, 'relative_path': relative_path_str,
                                'item_name': dirname, 'is_file': False, 'size': None, 
                                'lmt': stat_info.st_mtime, 'ct': stat_info.st_ctime, 'content_hash': None
                            })
                        except OSError as e:
                            self.progress_updated.emit(processed_items_count, total_files_to_scan_estimate, f"Error stating dir {full_path_obj}: {e}")
                        processed_items_count +=1

                    # Prepare (non-ignored) files for hashing
                    for filename in filenames:
                        full_path_obj = current_dir_path / filename
                        if self._is_ignored(filename, full_path_obj, False):
                            self.progress_updated.emit(processed_items_count, total_files_to_scan_estimate, f"Ignoring file: {filename}")
                            # total_files_to_scan_estimate may become inaccurate if we skip a lot here, but it's an estimate
                            processed_items_count +=1
                            continue 

                        if full_path_obj.is_symlink():
                             self.progress_updated.emit(processed_items_count, total_files_to_scan_estimate, f"Skipping symlink: {full_path_obj}")
                             processed_items_count +=1
                             continue
                        relative_path_str = full_path_obj.relative_to(root_folder_path).as_posix()
                        files_to_hash_map[str(full_path_obj)] = (root_idx, relative_path_str, filename)
                        # We will increment processed_items_count after hashing for files
            
            # Hash files in parallel
            paths_to_hash_list = list(files_to_hash_map.keys())
            path_hashes = {} 
            
            actual_files_to_hash_count = len(paths_to_hash_list)
            hashed_files_count = 0
            self.progress_updated.emit(0, actual_files_to_hash_count, "Hashing files...")


            if paths_to_hash_list:
                with ProcessPoolExecutor(max_workers=os.cpu_count() or 1) as executor:
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
                            # self.progress_updated.emit below will show filename
                        
                        hashed_files_count +=1
                        self.progress_updated.emit(hashed_files_count, actual_files_to_hash_count, f"Hashed: {Path(original_path_str).name}")
                        processed_items_count +=1


            # Populate file items with hashes
            for abs_path_str, (root_idx, rel_path_str, item_name) in files_to_hash_map.items():
                try:
                    stat_info = Path(abs_path_str).stat(follow_symlinks=False) 
                    snapshot_items_for_db.append({
                        'root_folder_idx': root_idx, 'relative_path': rel_path_str,
                        'item_name': item_name, 'is_file': True, 'size': stat_info.st_size,
                        'lmt': stat_info.st_mtime, 'ct': stat_info.st_ctime,
                        'content_hash': path_hashes.get(abs_path_str)
                    })
                except OSError as e:
                     self.progress_updated.emit(processed_items_count, total_files_to_scan_estimate, f"Error stating file {abs_path_str}: {e}")
                # processed_items_count already incremented during hashing loop for files

            if not snapshot_items_for_db and not self.root_folders:
                 self.snapshot_error.emit("No folders selected or no items found to snapshot.")
                 return
            if not snapshot_items_for_db and self.root_folders: 
                 self.progress_updated.emit(processed_items_count, total_files_to_scan_estimate, "Warning: Selected folders are empty or no accessible items found.")

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