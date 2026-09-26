"""Compatibility name for the user's original MCX paper engine (unchanged)."""
def main():
    import runpy
    from pathlib import Path
    runpy.run_path(str(Path(__file__).with_name("mcx_paper_v5.py")), run_name="__main__")

if __name__ == "__main__":
    main()
