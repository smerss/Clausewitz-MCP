"""Process backend selection.

Everything above this layer (api, game, the MCP tools) is platform-agnostic and
only sees the `Backend` / `Injector` interface chosen here.

  read/write   Linux: process_vm_readv / process_vm_writev
  calls        Linux: ptrace, stop every thread, hijack the main thread

Windows is not implemented yet. See the roadmap in the project README.
"""
import platform

if platform.system() == "Windows":
    raise NotImplementedError(
        "Clausewitz-MCP is Linux-only for now.\n"
        "The Windows port needs two things: a PE backend (ReadProcessMemory /\n"
        "CreateRemoteThread) and, the harder half. Regenerating every offset\n"
        "table against hoi4.exe, because the shipped tables were recovered from\n"
        "the Linux ELF. See the roadmap in README.md.")

from . import linux as backend                  # noqa: E402,F401

Backend = backend.Backend
Injector = backend.Injector
CallError = backend.CallError
find_pid = backend.find_pid
