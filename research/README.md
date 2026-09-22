# research/

None of this runs during normal use. It's how the tables in `calling/tables/` were made,
and how to remake them when Paradox ships a patch.

[`FINDINGS.md`](FINDINGS.md) is the real document here. Around 750 lines covering the
container layouts, the handle system, the command queue, the combat objects, the save
format, and a post-mortem of every crash and freeze along the way.

## How the tables were made

The binary is stripped, so nothing starts from a symbol name.

1. **`elf.py` and `rtti.py`** walk Itanium C++ RTTI: 22,479 typeinfo records, 18,882
   classes, each with its vtable. This is what turns an arbitrary pointer into "that's a
   `CArmy`".
2. **`funcs.py`** recovers 128,144 function boundaries from `.eh_frame_hdr`.
3. **`leascan.py` and `callscan.py`** build cross-reference maps: which function
   references which address, which string.
4. **`cmdscan.py` and `cmdfields.py`** find every `CCommand` subclass, its vtable slots
   and its size.
5. **`diploscan.py`** does the same for diplomatic action classes.
6. **`fieldmap.py`** (which lives in `calling/`) is the good one. The game's serializer
   writes field *names* alongside offsets, so struct layouts come out with the
   developers' own naming rather than guesses. It reads the token table directly, with no
   injected calls.

## Re-running after a patch

```bash
export HOI4_PATH="/path/to/Hearts of Iron IV"
python3 research/rtti.py        # writes rtti.pkl
python3 research/funcs.py
python3 research/leascan.py
python3 research/cmdscan.py
python3 research/diploscan.py
python3 clausewitz.py tables    # regenerate the text tables
```

Dumps land in `re-dumps/` and are gitignored.

## The Windows port

This is the main piece of missing work, and it's self-contained. Two halves.

**A PE backend.** `calling/process/` only has `linux.py`. A Windows sibling needs
`OpenProcess` plus `ReadProcessMemory` / `WriteProcessMemory` for memory, the module base
from `EnumProcessModulesEx`, and `VirtualAllocEx` plus `CreateRemoteThread` with a small
shellcode stub for calls. The x64 convention passes four arguments in registers, so the
stub has to marshal them. This half is mechanical.

**Regenerating the tables.** The slower half. Every offset in `calling/tables/` was
recovered from the Linux ELF, and `hoi4.exe` is a different binary. `elf.py` needs a
sibling that parses PE sections and the exception directory before any of the scanners
above produce valid Windows offsets.

If you want to take this on, open an issue first so nobody duplicates the work.

## Not scanners

`run_rage.py` and `run_plan.py` drive the combat engine and the opening plan against a
live game. Useful for watching a change land. They're experiments, not product, which is
why they live here.
