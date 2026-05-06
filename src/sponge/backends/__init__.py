"""Reference GraphBackend implementations bundled with Sponge.

`json_file.JsonFileBackend` is the default — a single graph.json file with
atomic writes and fcntl locking. Suitable for personal use up to ~10k nodes.
Beyond that, implement GraphBackend against a real database.
"""
from sponge.backends.json_file import JsonFileBackend

__all__ = ["JsonFileBackend"]
