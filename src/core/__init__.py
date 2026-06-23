"""核心算法包。"""
from .algorithms import (
    ALGORITHMS,
    apply_protection,
    chroma_key_black,
    color_key,
    threshold_black,
    unmult_black,
    unmult_color,
)
from .processor import process_file, process_folder

__all__ = [
    "ALGORITHMS",
    "unmult_black",
    "unmult_color",
    "color_key",
    "threshold_black",
    "chroma_key_black",
    "apply_protection",
    "process_file",
    "process_folder",
]
