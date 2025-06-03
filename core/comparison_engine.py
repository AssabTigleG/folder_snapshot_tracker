# core/comparison_engine.py
import os
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple 
from PyQt6.QtCore import QThread, pyqtSignal
from concurrent.futures import ProcessPoolExecutor, as_completed, CancelledError
import fnmatch 

from utils.hashing_utils import calculate_sha256_for_file
from core.db_manager import DatabaseManager

class ComparisonWorker(QThread):
    comparison_progress = pyqtSignal(int, int, str) 
    comparison_complete = pyqtSignal(dict) 
    comparison_error = pyqtSignal(str)

    def __init__(self, snapshot_id_to_compare: int, 
                 ignore_patterns: Optional[List[str]] = None):
        super().__init__()
        self.snapshot_id = snapshot_id_to_compare
        self.db_manager = DatabaseManager()
        self.ignore_patterns = ignore_patterns or []
        self.current_root_for_item: Optional[Path] = None
        self._is_cancellation_requested = False
        self.live_scan_executor: Optional[ProcessPoolExecutor] = None # Executor for live scan hashing

    def request_cancellation(self):
        self._is_cancellation_requested = True
        self.comparison_progress.emit(0,0, "Cancellation requested...")
        if self.live_scan_executor:
            try:
                self.live_scan_executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                self.live_scan_executor.shutdown(wait=False)
            except Exception: pass

    def _is_ignored(self, item_name: str, item_full_path_obj: Path, is_dir: bool) -> bool:
        # (Identical to SnapshotWorker._is_ignored)
        for pattern in self.ignore_patterns:
            pattern_lower = pattern.lower()
            item_name_lower = item_name.lower()
            if fnmatch.fnmatchcase(item_name_lower, pattern_lower): return True
            if not is_dir and ('*' in pattern or '?' in pattern or '[' in pattern):
                if fnmatch.fnmatchcase(item_name_lower, pattern_lower): return True
            if is_dir and pattern.endswith('/'):
                if fnmatch.fnmatchcase(item_name_lower, pattern_lower[:-1]): return True
        return False

    def _scan_live_folder_state(self, root_folders_paths_str: List[str]) -> Optional[List[Dict[str, Any]]]:
        live_items = []
        files_to_hash_map = {}
        root_folders = [Path(p) for p in root_folders_paths_str]

        # --- Estimation for Live Scan ---
        self.comparison_progress.emit(0, 0, "Estimating live scan work...")
        total_items_to_potentially_scan = 0
        for root_idx, root_path in enumerate(root_folders):
            if self._is_cancellation_requested: return None
            if root_path.is_dir():
                try:
                    for dirpath, dirnames, filenames in os.walk(root_path):
                        if self._is_cancellation_requested: break
                        total_items_to_potentially_scan += len(dirnames) + len(filenames)
                        if total_items_to_potentially_scan % 500 == 0:
                             self.comparison_progress.emit(root_idx + 1, len(root_folders), f"Estimating live: {root_path.name}...")
                except OSError: pass
            if self._is_cancellation_requested: break
        if self._is_cancellation_requested: return None

        self.comparison_progress.emit(0, total_items_to_potentially_scan, "Scanning live state...")
        processed_scan_items_count = 0

        # --- Detailed Live Scan ---
        for root_idx, root_folder_path in enumerate(root_folders):
            if self._is_cancellation_requested: return None
            self.current_root_for_item = root_folder_path
            if not root_folder_path.is_dir():
                self.comparison_progress.emit(processed_scan_items_count, total_items_to_potentially_scan, f"Skipping non-dir (live): {root_folder_path}")
                continue

            for dirpath, dirnames_orig, filenames in os.walk(root_folder_path, topdown=True):
                if self._is_cancellation_requested: break
                dirnames_mutable = list(dirnames_orig)
                for i in range(len(dirnames_mutable) - 1, -1, -1):
                    # ... (ignore logic for dirs, same as SnapshotWorker) ...
                    if self._is_cancellation_requested: break
                    dirname = dirnames_mutable[i]
                    dir_full_path_obj = Path(dirpath) / dirname
                    if self._is_ignored(dirname, dir_full_path_obj, True):
                        del dirnames_mutable[i] 
                        if i < len(dirnames_orig): del dirnames_orig[i]
                    processed_scan_items_count += 1
                    if processed_scan_items_count % 100 == 0:
                        self.comparison_progress.emit(processed_scan_items_count, total_items_to_potentially_scan, f"Scanning live: {dir_full_path_obj.name}")
                if self._is_cancellation_requested: break
                
                current_dir_path = Path(dirpath)
                for dirname in dirnames_mutable:
                    # ... (add dir to live_items, same as SnapshotWorker adds to snapshot_items_for_db) ...
                    if self._is_cancellation_requested: break
                    full_path_obj = current_dir_path / dirname
                    try:
                        stat_info = full_path_obj.stat(follow_symlinks=False)
                        live_items.append({
                            'root_folder_idx': root_idx, 'relative_path': full_path_obj.relative_to(root_folder_path).as_posix(),
                            'item_name': dirname, 'is_file': False, 'size': None,
                            'lmt': stat_info.st_mtime, 'ct': stat_info.st_ctime, 'content_hash': None
                        })
                    except OSError: pass
                if self._is_cancellation_requested: break

                for filename in filenames:
                    # ... (ignore logic for files, symlink check, add to files_to_hash_map, same as SnapshotWorker) ...
                    if self._is_cancellation_requested: break
                    full_path_obj = current_dir_path / filename
                    processed_scan_items_count += 1
                    if self._is_ignored(filename, full_path_obj, False) or full_path_obj.is_symlink():
                        if processed_scan_items_count % 100 == 0:
                             self.comparison_progress.emit(processed_scan_items_count, total_items_to_potentially_scan, f"Scanning/Skipping live: {filename}")
                        continue
                    files_to_hash_map[str(full_path_obj)] = (root_idx, full_path_obj.relative_to(root_folder_path).as_posix(), filename)
                    if processed_scan_items_count % 100 == 0:
                        self.comparison_progress.emit(processed_scan_items_count, total_items_to_potentially_scan, f"Queued live hash: {filename}")
                if self._is_cancellation_requested: break
            if self._is_cancellation_requested: return None
        if self._is_cancellation_requested: return None

        # --- Hashing Live Files ---
        paths_to_hash_list = list(files_to_hash_map.keys())
        path_hashes = {} 
        actual_files_to_hash_count = len(paths_to_hash_list)
        hashed_files_count = 0
        self.comparison_progress.emit(0, actual_files_to_hash_count, "Hashing live files...")

        if paths_to_hash_list:
            self.live_scan_executor = ProcessPoolExecutor(max_workers=os.cpu_count() or 1)
            try:
                future_to_path = {
                    self.live_scan_executor.submit(calculate_sha256_for_file, path_str): path_str
                    for path_str in paths_to_hash_list
                }
                for future in as_completed(future_to_path):
                    original_path_str = future_to_path[future]
                    if self._is_cancellation_requested:
                        for f_key in future_to_path:
                            if not f_key.done() and not f_key.cancelled(): f_key.cancel()
                        return None 
                    try:
                        file_hash = future.result()
                        path_hashes[original_path_str] = file_hash
                    except CancelledError:
                        path_hashes[original_path_str] = "CANCELLED_HASH_LIVE"
                    except Exception:
                        path_hashes[original_path_str] = "ERROR_HASHING_LIVE"
                    
                    hashed_files_count +=1
                    self.comparison_progress.emit(hashed_files_count, actual_files_to_hash_count, f"Hashed live: {Path(original_path_str).name}")
            except Exception: # Error in executor submission or management
                if not self._is_cancellation_requested:
                    # Log or emit this error, but still try to cleanup
                    self.comparison_progress.emit(0,0, "Error in live hashing process setup.")
                return None # Cannot proceed
            finally:
                if self.live_scan_executor:
                    try: self.live_scan_executor.shutdown(wait=False, cancel_futures=True)
                    except TypeError: self.live_scan_executor.shutdown(wait=False)
                    except Exception: pass
                    self.live_scan_executor = None # Clear after use
        
        if self._is_cancellation_requested: return None

        self.comparison_progress.emit(0,0, "Finalizing live scan data...")
        for abs_path_str, (root_idx, rel_path_str, item_name) in files_to_hash_map.items():
            if self._is_cancellation_requested: break
            try:
                stat_info = Path(abs_path_str).stat(follow_symlinks=False)
                live_items.append({
                    'root_folder_idx': root_idx, 'relative_path': rel_path_str,
                    'item_name': item_name, 'is_file': True, 'size': stat_info.st_size,
                    'lmt': stat_info.st_mtime, 'ct': stat_info.st_ctime,
                    'content_hash': path_hashes.get(abs_path_str)
                })
            except OSError: pass
        if self._is_cancellation_requested: return None
        return live_items


    def run(self):
        try:
            all_snapshots = self.db_manager.get_all_snapshots()
            target_snapshot_info = next((s for s in all_snapshots if s['id'] == self.snapshot_id), None)

            if not target_snapshot_info:
                self.comparison_error.emit(f"Snapshot with ID {self.snapshot_id} not found.")
                return
            if self._is_cancellation_requested:
                self.comparison_error.emit("Operation cancelled before live scan.")
                return

            root_folders_for_snapshot = target_snapshot_info['root_folders']
            live_items_list = self._scan_live_folder_state(root_folders_for_snapshot)

            if self._is_cancellation_requested or live_items_list is None: 
                self.comparison_error.emit("Operation cancelled during live folder scan.")
                return
            
            stored_items_list = self.db_manager.get_snapshot_items(self.snapshot_id)
            if self._is_cancellation_requested:
                self.comparison_error.emit("Operation cancelled before data processing.")
                return

            stored_data = {(item['root_folder_idx'], item['relative_path']): item for item in stored_items_list}
            live_data = {(item['root_folder_idx'], item['relative_path']): item for item in live_items_list}

            stored_keys = set(stored_data.keys())
            live_keys = set(live_data.keys())

            added_keys = live_keys - stored_keys
            deleted_keys = stored_keys - live_keys
            common_keys = stored_keys.intersection(live_keys)

            results = {'added': [], 'deleted': [], 'modified': []}
            
            total_comparisons = len(added_keys) + len(deleted_keys) + len(common_keys)
            processed_comparisons = 0
            self.comparison_progress.emit(0, total_comparisons, "Comparing items...")

            for key_idx, key in enumerate(added_keys):
                if self._is_cancellation_requested: break
                results['added'].append(live_data[key])
                processed_comparisons +=1
                if key_idx % 50 == 0 : self.comparison_progress.emit(processed_comparisons, total_comparisons, f"Comparing added: {live_data[key]['item_name']}")
            if self._is_cancellation_requested: self.comparison_error.emit("Cancelled during added item processing."); return

            for key_idx, key in enumerate(deleted_keys):
                if self._is_cancellation_requested: break
                results['deleted'].append(stored_data[key])
                processed_comparisons +=1
                if key_idx % 50 == 0 : self.comparison_progress.emit(processed_comparisons, total_comparisons, f"Comparing deleted: {stored_data[key]['item_name']}")
            if self._is_cancellation_requested: self.comparison_error.emit("Cancelled during deleted item processing."); return

            for key_idx, key in enumerate(common_keys):
                if self._is_cancellation_requested: break
                item_stored = stored_data[key]
                item_live = live_data[key]
                modified = False
                # ... (Modification logic as before) ...
                if item_stored['is_file'] != item_live['is_file']: modified = True
                elif item_stored['is_file']: 
                    sh1, sh2 = item_stored.get('content_hash'), item_live.get('content_hash')
                    vh1, vh2 = isinstance(sh1, str) and not sh1.startswith("ERROR"), isinstance(sh2, str) and not sh2.startswith("ERROR")
                    if vh1 and vh2 and sh1 != sh2: modified = True
                    elif not modified and (item_stored.get('lmt') != item_live.get('lmt') or item_stored.get('size') != item_live.get('size')): modified = True
                if modified: results['modified'].append({'old': item_stored, 'new': item_live})
                processed_comparisons +=1
                if key_idx % 50 == 0 : self.comparison_progress.emit(processed_comparisons, total_comparisons, f"Comparing common: {item_live['item_name']}")
            if self._is_cancellation_requested: self.comparison_error.emit("Cancelled during modified item processing."); return
            
            self.comparison_complete.emit(results)

        except Exception as e:
            if not self._is_cancellation_requested:
                self.comparison_error.emit(f"Unexpected error in comparison worker: {e}")
            # import traceback; traceback.print_exc()
        finally:
            if self.live_scan_executor: # Cleanup executor if used in _scan_live_folder_state
                try: self.live_scan_executor.shutdown(wait=True, cancel_futures=True)
                except TypeError: self.live_scan_executor.shutdown(wait=True)
                except Exception: pass
                self.live_scan_executor = None