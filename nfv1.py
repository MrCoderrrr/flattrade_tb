"""Compatibility name for the user's original NIFTY paper engine (unchanged).

This legacy launcher has its original side effects and risk rules. It is not
started by the new dashboard. Importing this alias does not run the engine.
"""
def main():
    from nifty_paper_v3 import main as original_main
    original_main()

if __name__ == "__main__":
    main()
