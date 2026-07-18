"""Optional LLM-assisted description matching (M6).

This is the ONLY layer in the app allowed to make network/LLM calls. It is a
pre-processing step that runs BEFORE `app.matching.engine.reconcile()`: it
never decides amounts or dates, and it never runs unless the user explicitly
enables it (off by default, per CLAUDE.md Hard Rule #7 and REQUIREMENTS F3.4).
"""
