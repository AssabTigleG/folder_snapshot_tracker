# utils/config_handler.py
import json
from pathlib import Path
from typing import List, Dict

# utils/config_handler.py
import json
from pathlib import Path
from typing import List, Dict

CONFIG_FILE_NAME = "app_config.json"
DEFAULT_IGNORE_PATTERNS = [
    "*.pyc",
    "__pycache__",  
    ".DS_Store",
    "Thumbs.db",
    ".git",         
    ".vscode",      
    "node_modules"  
]

class ConfigManager:
    def __init__(self, config_dir: Path = Path(".")): # Saves config in app's root dir
        self.config_file = config_dir / CONFIG_FILE_NAME
        self.config_data: Dict = self._load_config()

    def _load_config(self) -> Dict:
        default_config = {
            "ignore_patterns": list(DEFAULT_IGNORE_PATTERNS), # Return copy
            "snapshot_root_folders": [] # New default entry
        }
        
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    loaded_data = json.load(f)
                    # Merge loaded data with defaults to ensure all keys exist
                    return {**default_config, **loaded_data}
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error loading config file {self.config_file}: {e}. Using defaults.")
        
        # If file doesn't exist or loading failed, return defaults
        return default_config

    def save_config(self):
        try:
            # Ensure directory exists before saving
            self.config_file.parent.mkdir(parents=True, exist_ok=True) 
            with open(self.config_file, 'w') as f:
                json.dump(self.config_data, f, indent=4)
        except IOError as e:
            print(f"Error saving config file {self.config_file}: {e}")

    def get_ignore_patterns(self) -> List[str]:
        # Ensure the key exists and return a copy
        return list(self.config_data.get("ignore_patterns", DEFAULT_IGNORE_PATTERNS))

    def set_ignore_patterns(self, patterns: List[str]):
        self.config_data["ignore_patterns"] = patterns
        self.save_config()

    def get_snapshot_root_folders(self) -> List[str]:
        # Ensure the key exists, ensure items are strings, and return a copy
        # Storing as strings is safer for JSON
        return [str(p) for p in self.config_data.get("snapshot_root_folders", [])]

    def set_snapshot_root_folders(self, folders: List[str]):
        # Store as strings
        self.config_data["snapshot_root_folders"] = [str(p) for p in folders]
        self.save_config()

    def save_config(self):
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config_data, f, indent=4)
        except IOError as e:
            print(f"Error saving config file {self.config_file}: {e}")

    def get_ignore_patterns(self) -> List[str]:
        return self.config_data.get("ignore_patterns", list(DEFAULT_IGNORE_PATTERNS)) # Return copy

    def set_ignore_patterns(self, patterns: List[str]):
        self.config_data["ignore_patterns"] = patterns
        self.save_config()