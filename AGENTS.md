# Agent notes for Annealage Ember

## Documentation

Every feature update or behaviour change must come with its documentation updated, where it applies. That means the `README.md` section for the surface it touches, the module docstring that holds the design (for the session backends that is the top of `session/omp.py`, `session/sdk.py` or `session/codex.py`), and the entry in `~/studio/planning/agent-package.md` when it changes the shared agent layer's design or progress. Do it in the same commit as the change, not as a follow-up.

All documentation, including README text, module docstrings and planning entries, must be written or revised using the `writing-style` skill (`/skill:writing-style`). Docs are formal technical writing: lead with the point, say what the code does and why, use Australian spelling, no emdashes, no closing summaries.
