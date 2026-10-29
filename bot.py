"""Compatibility shim: the real entry point is main.py (python main.py)."""
import runpy

if __name__ == "__main__":
    runpy.run_path("main.py", run_name="__main__")
