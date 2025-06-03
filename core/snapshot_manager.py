# core/snapshot_manager.py
import os
from pathlib import Path
import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed, CancelledError
from typing import List, Dict, Any, Optional 
from PyQt6.QtCore import QThread, pyqtSignal
import fnmatch 

from utils.hashing_utils import calculate_sha256_for_file
from core.db_manager import DatabaseManager

class SnapshotWorker(QThread):
    progress_updated = pyqtSignal(int, int, str)
    snapshot_complete = pyqtSignal(int, str)
    snapshot_error = pyqtSignal(str)

    def __init__(self, root_folders: List[str], 
                 snapshot_name: Optional[str] = None, 
                 ignore_patterns: Optional[List[str]] = None):
        super().__init__()
        self.root_folders = [Path(p) for p in root_folders]
        self.snapshot_name = snapshot_name or f"Snapshot_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.db_manager = DatabaseManager()
        self.ignore_patterns = ignore_patterns or []
        self.current_root_for_item: Optional[Path] = None
        self._is_cancellation_requested = False
        self.process_pool_executor: Optional[ProcessPoolExecutor] = None

    def request_cancellation(self):
        self._is_cancellation_requested = True
        self.progress_updated.emit(0,0, "Cancellation requested...") # Update status
        if self.process_pool_executor:
            try:
                self.process_pool_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError: # For Python < 3.9
                self.process_pool_executor.shutdown(wait=False)
            except Exception: # Broad catch if shutdown itself fails
                pass 

    def _is_ignored(self, item_name: str, item_full_path_obj: Path, is_dir: bool) -> bool:
        for pattern in self.ignore_patterns:
            pattern_lower = pattern.lower()
            item_name_lower = item_name.lower()
            if fnmatch.fnmatchcase(item_name_lower, pattern_lower): return True
            if not is_dir and ('*' in pattern or '?' in pattern or '[' in pattern):
                if fnmatch.fnmatchcase(item_name_lower, pattern_lower): return True
            if is_dir and pattern.endswith('/'):
                if fnmatch.fnmatchcase(item_name_lower, pattern_lower[:-1]): return True
        return False

    def run(self):
        try:
            snapshot_items_for_db = []
            files_to_hash_map = {} 

            # --- Estimation Phase ---
            self.progress_updated.emit(0, 0, "Estimating work (can take time)...")
            total_items_to_potentially_scan = 0
            for root_idx, root_path in enumerate(self.root_folders):
                if self._is_cancellation_requested:
                    self.snapshot_error.emit("Operation cancelled during estimation.")
                    return
                if root_path.is_dir():
                    try:
                        for dirpath, dirnames, filenames in os.walk(root_path):
                            if self._is_cancellation_requested: break
                            total_items_to_potentially_scan += len(dirnames) + len(filenames)
                            # Periodically update UI during long estimation
                            if total_items_to_potentially_scan % 500 == 0:
                                self.progress_updated.emit(root_idx + 1, len(self.root_folders), f"Estimating in {root_path.name} ({total_items_to_potentially_scan} items so far)...")
                    except OSError: pass
                if self._is_cancellation_requested: break # From outer loop
            if self._is_cancellation_requested:
                self.snapshot_error.emit("Operation cancelled after estimation.")
                return
            
            self.progress_updated.emit(0, total_items_to_potentially_scan, "Starting detailed scan...")
            processed_scan_items_count = 0

            # --- Detailed Scan Phase ---
            for root_idx, root_folder_path in enumerate(self.root_folders):
                if self._is_cancellation_requested:
                    self.snapshot_error.emit("Operation cancelled during folder scan.")
                    return
                self.current_root_for_item = root_folder_path
                if not root_folder_path.is_dir():
                    self.progress_updated.emit(processed_scan_items_count, total_items_to_potentially_scan, f"Skipping non-dir: {root_folder_path}")
                    continue

                for dirpath, dirnames_orig, filenames in os.walk(root_folder_path, topdown=True):
                    if self._is_cancellation_requested: break
                    dirnames_mutable = list(dirnames_orig)

                    for i in range(len(dirnames_mutable) - 1, -1, -1):
                        if self._is_cancellation_requested: break
                        dirname = dirnames_mutable[i]
                        dir_full_path_obj = Path(dirpath) / dirname
                        if self._is_ignored(dirname, dir_full_path_obj, True):
                            del dirnames_mutable[i] 
                            if i < len(dirnames_orig): del dirnames_orig[i]
                        processed_scan_items_count += 1
                        if processed_scan_items_count % 100 == 0:
                            self.progress_updated.emit(processed_scan_items_count, total_items_to_potentially_scan, f"Scanning: {dir_full_path_obj.name}")
                    if self._is_cancellation_requested: break

                    current_dir_path = Path(dirpath)
                    for dirname in dirnames_mutable: 
                        if self._is_cancellation_requested: break
                        full_path_obj = current_dir_path / dirname
                        # ... (add dir to snapshot_items_for_db as before) ...
                        try:
                            stat_info = full_path_obj.stat(follow_symlinks=False)
                            snapshot_items_for_db.append({
                                'root_folder_idx': root_idx, 'relative_path': full_path_obj.relative_to(root_folder_path).as_posix(),
                                'item_name': dirname, 'is_file': False, 'size': None, 
                                'lmt': stat_info.st_mtime, 'ct': stat_info.st_ctime, 'content_hash': None
                            })
                        except OSError: pass # Log error if needed
                        # processed_scan_items_count already updated for dirs

                    for filename in filenames:
                        if self._is_cancellation_requested: break
                        full_path_obj = current_dir_path / filename
                        processed_scan_items_count += 1
                        if self._is_ignored(filename, full_path_obj, False) or full_path_obj.is_symlink():
                            if processed_scan_items_count % 100 == 0:
                                self.progress_updated.emit(processed_scan_items_count, total_items_to_potentially_scan, f"Scanning/Skipping: {filename}")
                            continue
                        files_to_hash_map[str(full_path_obj)] = (root_idx, full_path_obj.relative_to(root_folder_path).as_posix(), filename)
                        if processed_scan_items_count % 100 == 0:
                             self.progress_updated.emit(processed_scan_items_count, total_items_to_potentially_scan, f"Queued for hash: {filename}")
                    if self._is_cancellation_requested: break
                if self._is_cancellation_requested: break 
            if self._is_cancellation_requested:
                self.snapshot_error.emit("Operation cancelled before hashing.")
                return

            # --- Hashing Phase ---
            paths_to_hash_list = list(files_to_hash_map.keys())
            path_hashes = {} 
            actual_files_to_hash_count = len(paths_to_hash_list)
            hashed_files_count = 0
            self.progress_updated.emit(0, actual_files_to_hash_count, "Starting file hashing...")

            if paths_to_hash_list:
                self.process_pool_executor = ProcessPoolExecutor(max_workers=os.cpu_count() or 1)
                try:
                    future_to_path = {
                        self.process_pool_executor.submit(calculate_sha256_for_file, path_str): path_str
                        for path_str in paths_to_hash_list
                    }
                    for future in as_completed(future_to_path):
                        original_path_str = future_to_path[future]
                        if self._is_cancellation_requested: # Check before .result()
                            # Cancel remaining futures more proactively
                            for f_key in future_to_path: # future_to_path.keys() is futures
                                if not f_key.done() and not f_key.cancelled():
                                    f_key.cancel()
                            self.snapshot_error.emit("Operation cancelled during hashing.")
                            return 
                        try:
                            file_hash = future.result() 
                            path_hashes[original_path_str] = file_hash
                        except CancelledError:
                            path_hashes[original_path_str] = "CANCELLED_HASH"
                        except Exception:
                            path_hashes[original_path_str] = "ERROR_HASHING_FUTURE"
                        
                        hashed_files_count +=1
                        self.progress_updated.emit(hashed_files_count, actual_files_to_hash_count, f"Hashed: {Path(original_path_str).name}")
                except Exception as e: 
                    if not self._is_cancellation_requested: 
                        self.snapshot_error.emit(f"Error during hashing process: {e}")
                    return 
                finally: # Ensure executor is cleaned up if hashing block is exited
                    if self.process_pool_executor:
                        try: self.process_pool_executor.shutdown(wait=False, cancel_futures=True) # cancel_futures for good measure
                        except TypeError: self.process_pool_executor.shutdown(wait=False)
                        except Exception: pass
                        self.process_pool_executor = None
            
            if self._is_cancellation_requested: 
                self.snapshot_error.emit("Operation cancelled after hashing attempt.")
                return

            # --- Populate and Save ---
            self.progress_updated.emit(0,0, "Finalizing snapshot data...")
            for abs_path_str, (root_idx, rel_path_str, item_name) in files_to_hash_map.items():
                if self._is_cancellation_requested: break
                try:
                    stat_info = Path(abs_path_str).stat(follow_symlinks=False) 
                    snapshot_items_for_db.append({
                        'root_folder_idx': root_idx, 'relative_path': rel_path_str,
                        'item_name': item_name, 'is_file': True, 'size': stat_info.st_size,
                        'lmt': stat_info.st_mtime, 'ct': stat_info.st_ctime,
                        'content_hash': path_hashes.get(abs_path_str)
                    })
                except OSError: pass 
            if self._is_cancellation_requested:
                self.snapshot_error.emit("Operation cancelled before saving to database.")
                return

            if not snapshot_items_for_db and not self.root_folders:
                 self.snapshot_error.emit("No folders selected or no items found to snapshot (after ignores).")
                 return
            if not snapshot_items_for_db and self.root_folders: 
                 self.progress_updated.emit(0,0, "Warning: No items to snapshot (empty or all ignored).")

            snapshot_id = self.db_manager.add_snapshot_record(
                self.snapshot_name, datetime.datetime.now().isoformat(), [str(p) for p in self.root_folders] 
            )
            if snapshot_items_for_db:
                self.db_manager.batch_insert_snapshot_items(snapshot_id, snapshot_items_for_db)
            
            self.snapshot_complete.emit(snapshot_id, self.snapshot_name)

        except Exception as e: 
            if not self._is_cancellation_requested: # Avoid double error if already handled
                 self.snapshot_error.emit(f"Unexpected error in snapshot worker: {e}")
            # import traceback; traceback.print_exc() # For debugging
        finally:
            if self.process_pool_executor: # Final cleanup
                try: self.process_pool_executor.shutdown(wait=True, cancel_futures=True) # Wait here ensures cleanup before thread exits
                except TypeError: self.process_pool_executor.shutdown(wait=True)
                except Exception: pass
                self.process_pool_executor = None