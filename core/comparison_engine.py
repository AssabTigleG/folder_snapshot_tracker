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

    def __init__(self,
                 snapshot_id_A: int,
                 snapshot_id_B: Optional[int] = None, # For snapshot vs snapshot
                 comparison_mode: str = "live_vs_snapshot", # "live_vs_snapshot" or "snapshot_vs_snapshot"
                 ignore_patterns: Optional[List[str]] = None,
                 quick_compare: bool = False,
                 trust_metadata_for_unchanged_files: bool = False):
        super().__init__()
        self.snapshot_id_A = snapshot_id_A
        self.snapshot_id_B = snapshot_id_B
        self.comparison_mode = comparison_mode # "live_vs_snapshot" or "snapshot_vs_snapshot"
        self.db_manager = DatabaseManager()
        self.ignore_patterns = ignore_patterns or []
        self.quick_compare = quick_compare
        # trust_metadata is primarily for live_vs_snapshot. Its direct applicability to snap_vs_snap is limited
        # as hashes are already stored. We'll consider its effect if LMT/size match in snap-vs-snap.
        self.trust_metadata_for_unchanged_files = trust_metadata_for_unchanged_files
        self.current_root_for_item: Optional[Path] = None # Used in live scan
        self._is_cancellation_requested = False
        self.live_scan_executor: Optional[ProcessPoolExecutor] = None 

    def request_cancellation(self):
        self._is_cancellation_requested = True
        self.comparison_progress.emit(0,0, "Cancellation requested...")
        if self.live_scan_executor:
            try:
                # For ProcessPoolExecutor, cancel_futures is available in Python 3.9+
                # For older versions, shutdown(wait=False) is the best we can do to stop new tasks
                if hasattr(self.live_scan_executor, 'shutdown') and callable(getattr(self.live_scan_executor, 'shutdown')):
                    try: # Attempt Python 3.9+ way
                        self.live_scan_executor.shutdown(wait=False, cancel_futures=True)
                    except TypeError: # Fallback for older Python
                        self.live_scan_executor.shutdown(wait=False)
            except Exception: pass # Best effort to shutdown

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

    def _scan_live_folder_state(self, root_folders_paths_str: List[str],
                                stored_data_map: Optional[Dict[Tuple[int, str], Dict[str, Any]]] = None
                                ) -> Optional[List[Dict[str, Any]]]:
        live_items = []
        files_to_hash_map = {}
        root_folders = [Path(p) for p in root_folders_paths_str]

        # Phase: Estimation
        self.comparison_progress.emit(0, 0, "Phase 1/3: Estimating live file system scan...")
        total_items_to_potentially_scan = 0
        for root_idx_est, root_path_est in enumerate(root_folders):
            if self._is_cancellation_requested: return None
            if root_path_est.is_dir():
                try:
                    for _, dirnames, filenames in os.walk(root_path_est):
                        if self._is_cancellation_requested: break
                        total_items_to_potentially_scan += len(dirnames) + len(filenames)
                        if total_items_to_potentially_scan % 750 == 0: # Adjusted update frequency
                             self.comparison_progress.emit(root_idx_est + 1, len(root_folders), f"Estimating in {root_path_est.name}...")
                except OSError: pass
            if self._is_cancellation_requested: break
        if self._is_cancellation_requested: return None

        # Phase: Detailed Scan (Pre-hash)
        scan_phase_msg = "Phase 2/3: Scanning live file system"
        if not self.quick_compare and not (self.trust_metadata_for_unchanged_files and stored_data_map):
            scan_phase_msg += " and queuing files for hashing"
        elif not self.quick_compare and self.trust_metadata_for_unchanged_files and stored_data_map:
            scan_phase_msg += " (using trusted metadata where possible)"

        self.comparison_progress.emit(0, total_items_to_potentially_scan, scan_phase_msg + "...")
        processed_scan_items_count = 0

        for root_idx, root_folder_path in enumerate(root_folders):
            if self._is_cancellation_requested: return None
            self.current_root_for_item = root_folder_path
            if not root_folder_path.is_dir():
                self.comparison_progress.emit(processed_scan_items_count, total_items_to_potentially_scan, f"{scan_phase_msg} (Skipping non-dir: {root_folder_path})")
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
                        if i < len(dirnames_orig): del dirnames_orig[i] # Ensure original is also modified for os.walk
                    processed_scan_items_count += 1
                    if processed_scan_items_count % 200 == 0: # Adjusted update frequency
                        self.comparison_progress.emit(processed_scan_items_count, total_items_to_potentially_scan, f"{scan_phase_msg} (Dir: {dir_full_path_obj.name})")
                if self._is_cancellation_requested: break

                current_dir_path = Path(dirpath)
                for dirname in dirnames_mutable:
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
                    if self._is_cancellation_requested: break
                    full_path_obj = current_dir_path / filename
                    processed_scan_items_count += 1
                    if self._is_ignored(filename, full_path_obj, False) or full_path_obj.is_symlink():
                        if processed_scan_items_count % 200 == 0:
                             self.comparison_progress.emit(processed_scan_items_count, total_items_to_potentially_scan, f"{scan_phase_msg} (Skipping: {filename})")
                        continue

                    try:
                        stat_info_live = full_path_obj.stat(follow_symlinks=False)
                        live_item_base = {
                            'root_folder_idx': root_idx,
                            'relative_path': full_path_obj.relative_to(root_folder_path).as_posix(),
                            'item_name': filename, 'is_file': True, 'size': stat_info_live.st_size,
                            'lmt': stat_info_live.st_mtime, 'ct': stat_info_live.st_ctime
                        }

                        if self.quick_compare:
                            live_item_base['content_hash'] = "NOT_HASHED_QUICK_COMPARE"
                            live_items.append(live_item_base)
                            if processed_scan_items_count % 100 == 0:
                                 self.comparison_progress.emit(processed_scan_items_count, total_items_to_potentially_scan, f"{scan_phase_msg} (Quick scan: {filename})")
                        else: # Full compare logic
                            use_trusted_hash = False
                            if self.trust_metadata_for_unchanged_files and stored_data_map:
                                item_key = (root_idx, live_item_base['relative_path'])
                                stored_item = stored_data_map.get(item_key)
                                if stored_item and stored_item['is_file'] and \
                                   abs(stored_item.get('lmt', 0) - live_item_base['lmt']) < 1e-6 and \
                                   stored_item.get('size') == live_item_base['size']:
                                    live_item_base['content_hash'] = stored_item.get('content_hash', 'ERROR_TRUSTED_HASH_MISSING')
                                    live_items.append(live_item_base)
                                    use_trusted_hash = True
                                    if processed_scan_items_count % 100 == 0:
                                        self.comparison_progress.emit(processed_scan_items_count, total_items_to_potentially_scan, f"{scan_phase_msg} (Trusted: {filename})")

                            if not use_trusted_hash:
                                files_to_hash_map[str(full_path_obj)] = (root_idx, live_item_base['relative_path'], filename)
                                if processed_scan_items_count % 100 == 0:
                                    self.comparison_progress.emit(processed_scan_items_count, total_items_to_potentially_scan, f"{scan_phase_msg} (Queued hash: {filename})")
                    except OSError:
                        if processed_scan_items_count % 100 == 0:
                            self.comparison_progress.emit(processed_scan_items_count, total_items_to_potentially_scan, f"{scan_phase_msg} (OS Error for: {filename})")
                        pass # Could log this error
                if self._is_cancellation_requested: break
            if self._is_cancellation_requested: return None
        if self._is_cancellation_requested: return None

        # Phase: Hashing (if applicable)
        if not self.quick_compare and files_to_hash_map:
            paths_to_hash_list = list(files_to_hash_map.keys())
            path_hashes = {}
            actual_files_to_hash_count = len(paths_to_hash_list)
            hashed_files_count = 0
            hashing_phase_msg = "Phase 3/3: Hashing live files"
            self.comparison_progress.emit(0, actual_files_to_hash_count, hashing_phase_msg + "...")

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
                        self.comparison_progress.emit(hashed_files_count, actual_files_to_hash_count, f"{hashing_phase_msg} ({Path(original_path_str).name})")
                except Exception:
                    if not self._is_cancellation_requested:
                        self.comparison_progress.emit(0,0, "Error in live hashing process setup.")
                    return None
                finally:
                    if self.live_scan_executor:
                        try:
                            if hasattr(self.live_scan_executor, 'shutdown') and callable(getattr(self.live_scan_executor, 'shutdown')):
                                try: self.live_scan_executor.shutdown(wait=False, cancel_futures=True)
                                except TypeError: self.live_scan_executor.shutdown(wait=False)
                        except Exception: pass
                        self.live_scan_executor = None

            if self._is_cancellation_requested: return None

            self.comparison_progress.emit(0,0, "Finalizing hashed live scan data...")
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
            self.comparison_progress.emit(0, 0, "Initializing comparison...")
            all_db_snapshots = self.db_manager.get_all_snapshots() # Get all once for efficiency

            # --- Data Acquisition Phase ---
            if self.comparison_mode == "live_vs_snapshot":
                target_snapshot_info = next((s for s in all_db_snapshots if s['id'] == self.snapshot_id_A), None)
                if not target_snapshot_info:
                    self.comparison_error.emit(f"Snapshot A (ID {self.snapshot_id_A}) not found.")
                    return
                if self._is_cancellation_requested: self.comparison_error.emit("Cancelled before loading Snapshot A."); return

                root_folders_for_snapshot = target_snapshot_info['root_folders']
                self.comparison_progress.emit(0, 0, "Phase 1/4: Loading Snapshot A data...")
                snapshot_A_items_list = self.db_manager.get_snapshot_items(self.snapshot_id_A)
                if self._is_cancellation_requested: self.comparison_error.emit("Cancelled while loading Snapshot A."); return
                
                stored_data = {(item['root_folder_idx'], item['relative_path']): item for item in snapshot_A_items_list}
                
                stored_data_map_for_scan = None
                if not self.quick_compare and self.trust_metadata_for_unchanged_files:
                    stored_data_map_for_scan = stored_data # Pass this to live scan

                # _scan_live_folder_state emits its own internal phase messages (e.g., Estimating, Scanning, Hashing)
                # These become overall Phase 2/4, 3/4 depending on hashing
                live_items_list = self._scan_live_folder_state(root_folders_for_snapshot, stored_data_map=stored_data_map_for_scan)
                if self._is_cancellation_requested or live_items_list is None:
                    self.comparison_error.emit("Operation cancelled during live folder scan.")
                    return
                
                data_from_A = stored_data
                data_from_B = {(item['root_folder_idx'], item['relative_path']): item for item in live_items_list}
                analysis_phase_prefix = "Phase 4/4" if not self.quick_compare else "Phase 3/3"

            elif self.comparison_mode == "snapshot_vs_snapshot":
                if self.snapshot_id_B is None:
                    self.comparison_error.emit("Snapshot B ID is missing for snapshot vs. snapshot comparison.")
                    return

                snapshot_A_info = next((s for s in all_db_snapshots if s['id'] == self.snapshot_id_A), None)
                snapshot_B_info = next((s for s in all_db_snapshots if s['id'] == self.snapshot_id_B), None)

                if not snapshot_A_info:
                    self.comparison_error.emit(f"Snapshot A (ID {self.snapshot_id_A}) not found.")
                    return
                if not snapshot_B_info:
                    self.comparison_error.emit(f"Snapshot B (ID {self.snapshot_id_B}) not found.")
                    return
                if self._is_cancellation_requested: self.comparison_error.emit("Cancelled before loading snapshots."); return
                
                # Check for root folder compatibility (optional, but good for meaningful comparison)
                # For now, we assume users know if comparison is meaningful.

                self.comparison_progress.emit(0, 0, "Phase 1/3: Loading Snapshot A data...")
                snapshot_A_items = self.db_manager.get_snapshot_items(self.snapshot_id_A)
                if self._is_cancellation_requested: self.comparison_error.emit("Cancelled while loading Snapshot A."); return
                
                self.comparison_progress.emit(0, 0, "Phase 2/3: Loading Snapshot B data...")
                snapshot_B_items = self.db_manager.get_snapshot_items(self.snapshot_id_B)
                if self._is_cancellation_requested: self.comparison_error.emit("Cancelled while loading Snapshot B."); return

                data_from_A = {(item['root_folder_idx'], item['relative_path']): item for item in snapshot_A_items}
                data_from_B = {(item['root_folder_idx'], item['relative_path']): item for item in snapshot_B_items}
                analysis_phase_prefix = "Phase 3/3"
            else:
                self.comparison_error.emit(f"Unknown comparison mode: {self.comparison_mode}")
                return

            if self._is_cancellation_requested: self.comparison_error.emit("Cancelled before analysis."); return

            # --- Analysis Phase ---
            keys_A = set(data_from_A.keys())
            keys_B = set(data_from_B.keys())

            # Items in B but not in A are "added" relative to A
            added_keys = keys_B - keys_A
            # Items in A but not in B are "deleted" relative to A
            deleted_keys = keys_A - keys_B
            common_keys = keys_A.intersection(keys_B)

            results = {'added': [], 'deleted': [], 'modified': []}
            
            total_comparisons = len(added_keys) + len(deleted_keys) + len(common_keys)
            processed_comparisons = 0
            analysis_phase_msg = f"{analysis_phase_prefix}: Analyzing differences"
            self.comparison_progress.emit(0, total_comparisons, analysis_phase_msg + "...")

            for key_idx, key in enumerate(added_keys):
                if self._is_cancellation_requested: break
                results['added'].append(data_from_B[key])
                processed_comparisons +=1
                if key_idx % 75 == 0 : self.comparison_progress.emit(processed_comparisons, total_comparisons, f"{analysis_phase_msg} (Found in B, not A: {data_from_B[key]['item_name']})")
            if self._is_cancellation_requested: self.comparison_error.emit("Cancelled during 'added' item processing."); return

            for key_idx, key in enumerate(deleted_keys):
                if self._is_cancellation_requested: break
                results['deleted'].append(data_from_A[key])
                processed_comparisons +=1
                if key_idx % 75 == 0 : self.comparison_progress.emit(processed_comparisons, total_comparisons, f"{analysis_phase_msg} (Found in A, not B: {data_from_A[key]['item_name']})")
            if self._is_cancellation_requested: self.comparison_error.emit("Cancelled during 'deleted' item processing."); return

            for key_idx, key in enumerate(common_keys):
                if self._is_cancellation_requested: break
                item_A = data_from_A[key]
                item_B = data_from_B[key]
                modified = False
                
                if item_A['is_file'] != item_B['is_file']:
                    modified = True # Type changed (file vs folder)
                elif item_A['is_file']: # Both are files
                    # LMT and Size comparison
                    lmt_differs = abs(item_A.get('lmt', 0) - item_B.get('lmt', 0)) > 1e-6
                    size_differs = item_A.get('size') != item_B.get('size')

                    if self.quick_compare:
                        if lmt_differs or size_differs:
                            modified = True
                    else: # Full compare (hash-based)
                        if lmt_differs or size_differs:
                            modified = True # Modified by metadata even in full compare
                        
                        # If metadata matches, or if modified status is still false, check hash
                        # In "snapshot_vs_snapshot", trust_metadata_for_unchanged_files might mean if LMT/size match, we don't flag as modified by hash
                        # if the hashes themselves were "trusted" (i.e. copied). But here, we have actual stored hashes.
                        if not modified or (modified and self.comparison_mode == "snapshot_vs_snapshot"): # always check hash if not quick_compare for snap_vs_snap
                            hash_A = item_A.get('content_hash')
                            hash_B = item_B.get('content_hash')

                            # Define valid hashes (not error states or placeholders)
                            valid_hash_A = isinstance(hash_A, str) and not hash_A.startswith("ERROR") and hash_A not in ["NOT_HASHED_QUICK_COMPARE", "CANCELLED_HASH_LIVE", "ERROR_TRUSTED_HASH_MISSING", None]
                            valid_hash_B = isinstance(hash_B, str) and not hash_B.startswith("ERROR") and hash_B not in ["NOT_HASHED_QUICK_COMPARE", "CANCELLED_HASH_LIVE", "ERROR_TRUSTED_HASH_MISSING", None]

                            if valid_hash_A and valid_hash_B:
                                if hash_A != hash_B:
                                    modified = True
                            elif valid_hash_A != valid_hash_B: # One is valid, the other is not (e.g. hashing error in one snapshot)
                                modified = True # Treat as modified due to hash discrepancy/problem
                            # If both hashes are problematic and metadata matched, not modified by this specific hash rule.
                            # If metadata already marked as modified, this hash check is for completeness of the 'new' record.
                # For folders, we don't compare content directly; presence/absence or type change is enough.
                
                if modified: results['modified'].append({'old': item_A, 'new': item_B})
                processed_comparisons +=1
                if key_idx % 75 == 0 : self.comparison_progress.emit(processed_comparisons, total_comparisons, f"{analysis_phase_msg} (Common: {item_B['item_name']})")
            if self._is_cancellation_requested: self.comparison_error.emit("Cancelled during 'modified' item processing."); return
            
            self.comparison_complete.emit(results)

        except Exception as e:
            if not self._is_cancellation_requested:
                # import traceback # For debugging
                # self.comparison_error.emit(f"Unexpected error: {e}\n{traceback.format_exc()}")
                self.comparison_error.emit(f"Unexpected error in comparison worker: {e}")
        finally:
            if self.live_scan_executor: # Only used in live_vs_snapshot mode
                try:
                    if hasattr(self.live_scan_executor, 'shutdown') and callable(getattr(self.live_scan_executor, 'shutdown')):
                        try: self.live_scan_executor.shutdown(wait=True, cancel_futures=True)
                        except TypeError: self.live_scan_executor.shutdown(wait=True)
                except Exception: pass
                self.live_scan_executor = None