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
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error loading config file {self.config_file}: {e}. Using defaults.")
        return {"ignore_patterns": DEFAULT_IGNORE_PATTERNS}

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