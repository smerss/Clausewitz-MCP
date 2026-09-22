"""Calling functions inside the target process. Platform-independent entry point.

The real work happens in the platform backend:
  Linux : ptrace stops every thread and hijacks the main one (process/linux.py)

Windows is not implemented yet; see the roadmap in the project README.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from process import Injector, CallError        # noqa: F401
