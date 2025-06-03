# core/comparison_engine.py
import os
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from PyQt6.QtCore import QThread, pyqtSignal
from concurrent.futures import ProcessPoolExecutor, as_completed
import fnmatch

from utils.hashing_utils import calculate_sha256_for_file
from core.db_manager import DatabaseManager

class ComparisonWorker(QThread):
    comparison_progress = pyqtSignal(int, int, str) # current, total, message
    comparison_complete = pyqtSignal(dict) # {'added': [], 'deleted': [], 'modified': []}
    comparison_error = pyqtSignal(str)

    def __init__(self, snapshot_id_to_compare: int, ignore_patterns: Optional[List[str]] = None):
        super().__init__()
        self.snapshot_id = snapshot_id_to_compare
        self.db_manager = DatabaseManager()
        self.ignore_patterns = ignore_patterns or []

    def _is_ignored(self, name: str, path: Path) -> bool:
        for pattern in self.ignore_patterns:
            if fnmatch.fnmatch(name, pattern):
                return True
        return False

    def _scan_live_folder_state(self, root_folders_paths_str: List[str]) -> List[Dict[str, Any]]:
        """Scans the current state of root_folders, similar to snapshot creation but in-memory."""
        live_items = []
        files_to_hash_map = {} # Maps absolute_path_str to (root_folder_idx, relative_path_str, item_name)
        root_folders = [Path(p) for p in root_folders_paths_str]

        total_files_to_scan = 0
        for root_path in root_folders:
             if root_path.is_dir():
                for _, _, files in os.walk(root_path):
                    total_files_to_scan += len(files)
        
        self.comparison_progress.emit(0, total_files_to_scan, "Scanning current state...")
        processed_files_count = 0

        for root_idx, root_folder_path in enumerate(root_folders):
            if not root_folder_path.is_dir():
                self.comparison_progress.emit(processed_files_count, total_files_to_scan, f"Warning: Root path {root_folder_path} is not a directory. Skipping for live scan.")
                continue
            
            # Use a list for dirnames to allow modification
            for dirpath, dirnames_orig, filenames in os.walk(root_folder_path, topdown=True):
                dirnames = list(dirnames_orig)

                # Filter directories
                for i in range(len(dirnames) - 1, -1, -1):
                    dirname = dirnames[i]
                    dir_full_path = Path(dirpath) / dirname
                    if self._is_ignored(dirname, dir_full_path.relative_to(root_folder_path)):
                        self.comparison_progress.emit(processed_files_count, total_files_to_scan, f"Ignoring dir (live): {dirname}")
                        del dirnames[i]
                        del dirnames_orig[i]

                current_dir_path = Path(dirpath)
                for dirname in dirnames: # Non-ignored dirs
                    full_path = current_dir_path / dirname
                    relative_path = full_path.relative_to(root_folder_path).as_posix()
                    try:
                        stat_info = full_path.stat(follow_symlinks=False)
                        live_items.append({
                            'root_folder_idx': root_idx, 'relative_path': relative_path,
                            'item_name': dirname, 'is_file': False, 'size': None,
                            'lmt': stat_info.st_mtime, 'ct': stat_info.st_ctime, 'content_hash': None
                        })
                    except OSError: pass 

                for filename in filenames:
                    full_path = current_dir_path / filename
                    if self._is_ignored(filename, full_path.relative_to(root_folder_path)):
                        self.comparison_progress.emit(processed_files_count, total_files_to_scan, f"Ignoring file (live): {filename}")
                        total_files_to_scan -=1
                        if total_files_to_scan < 0: total_files_to_scan = 0
                        continue

                    if full_path.is_symlink(): 
                        self.comparison_progress.emit(processed_files_count, total_files_to_scan, f"Skipping symlink (live): {full_path}")
                        total_files_to_scan -=1
                        if total_files_to_scan < 0: total_files_to_scan = 0
                        continue
                    relative_path = full_path.relative_to(root_folder_path).as_posix()
                    files_to_hash_map[str(full_path)] = (root_idx, relative_path, filename)

        path_hashes = {}
        paths_to_hash_list = list(files_to_hash_map.keys())

        if paths_to_hash_list:
            with ProcessPoolExecutor(max_workers=os.cpu_count() or 1) as executor: # Ensure at least 1 worker
                future_to_path = {
                    executor.submit(calculate_sha256_for_file, path_str): path_str
                    for path_str in paths_to_hash_list
                }
                for future in as_completed(future_to_path):
                    original_path_str = future_to_path[future]
                    try:
                        path_hashes[original_path_str] = future.result()
                    except Exception:
                        path_hashes[original_path_str] = "ERROR_HASHING_LIVE"
                    processed_files_count +=1
                    self.comparison_progress.emit(processed_files_count, total_files_to_scan, f"Hashing live: {Path(original_path_str).name}")
        
        for abs_path_str, (root_idx, rel_path_str, item_name) in files_to_hash_map.items():
            try:
                stat_info = Path(abs_path_str).stat(follow_symlinks=False)
                live_items.append({
                    'root_folder_idx': root_idx,
                    'relative_path': rel_path_str,
                    'item_name': item_name,
                    'is_file': True,
                    'size': stat_info.st_size,
                    'lmt': stat_info.st_mtime,
                    'ct': stat_info.st_ctime,
                    'content_hash': path_hashes.get(abs_path_str)
                })
            except OSError: pass 
        return live_items

    def run(self):
        try:
            all_snapshots = self.db_manager.get_all_snapshots()
            target_snapshot_info = next((s for s in all_snapshots if s['id'] == self.snapshot_id), None)

            if not target_snapshot_info:
                self.comparison_error.emit(f"Snapshot with ID {self.snapshot_id} not found.")
                return

            stored_items_list = self.db_manager.get_snapshot_items(self.snapshot_id)
            
            root_folders_for_snapshot = target_snapshot_info['root_folders']
            live_items_list = self._scan_live_folder_state(root_folders_for_snapshot)

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

            for key in added_keys:
                results['added'].append(live_data[key])
                processed_comparisons +=1
                self.comparison_progress.emit(processed_comparisons, total_comparisons, f"Found added: {live_data[key]['item_name']}")


            for key in deleted_keys:
                results['deleted'].append(stored_data[key])
                processed_comparisons +=1
                self.comparison_progress.emit(processed_comparisons, total_comparisons, f"Found deleted: {stored_data[key]['item_name']}")

            for key in common_keys:
                item_stored = stored_data[key]
                item_live = live_data[key]
                modified = False

                if item_stored['is_file'] != item_live['is_file']: 
                    modified = True
                elif item_stored['is_file']: 
                    # Check hash first if both are strings and not "ERROR..."
                    sh1 = item_stored.get('content_hash')
                    sh2 = item_live.get('content_hash')
                    valid_hash1 = isinstance(sh1, str) and not sh1.startswith("ERROR")
                    valid_hash2 = isinstance(sh2, str) and not sh2.startswith("ERROR")

                    if valid_hash1 and valid_hash2:
                        if sh1 != sh2:
                            modified = True
                    # Fallback to LMT/size if hashes couldn't be reliably compared or are identical
                    if not modified and (item_stored.get('lmt') != item_live.get('lmt') or item_stored.get('size') != item_live.get('size')):
                         modified = True
                
                if modified:
                    results['modified'].append({'old': item_stored, 'new': item_live})
                processed_comparisons +=1
                self.comparison_progress.emit(processed_comparisons, total_comparisons, f"Compared: {item_live['item_name']}")

            self.comparison_complete.emit(results)

        except Exception as e:
            self.comparison_error.emit(f"Comparison failed: {e}")
            import traceback
            traceback.print_exc()