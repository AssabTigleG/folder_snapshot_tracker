# utils/hashing_utils.py
import hashlib
import os

def calculate_sha256_for_file(filepath_str: str) -> str:
    """Calculates SHA256 hash for a file, reading in chunks."""
    sha256_hash = hashlib.sha256()
    try:
        with open(filepath_str, "rb") as f:
            for byte_block in iter(lambda: f.read(65536), b""):  # 64KB chunks
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except (IOError, OSError) as e:
        print(f"Error hashing {filepath_str}: {e}")
        return "ERROR_HASHING" # Or re-raise a specific exception