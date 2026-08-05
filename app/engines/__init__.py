"""Sync engines and the rclone plumbing they share.

M1 provides the subprocess primitive, remote definition rendering, and the
inspection commands that connection testing and capability probing need.

`base.py` (the SyncEngine interface), `rclone.py` (RcloneEngine) and `parsers.py`
(--combined and --use-json-log parsing) arrive with M2.
"""
