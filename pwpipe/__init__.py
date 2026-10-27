"""Paperwala paper-cleaning pipeline: find and remove ads, watermarks and
contact numbers from exam-paper PDFs without touching the paper itself.

The core (this package) works on local files only. Drive, Turso and GitHub
Actions wrap around it later; nothing here needs a network or a credential.
"""

__version__ = "0.1.0"
