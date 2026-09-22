#!/bin/bash
# dis.sh <start_vaddr_hex> [end_vaddr_hex]
BIN="/mnt/C/Program Files (x86)/Steam/steamapps/common/Hearts of Iron IV/hoi4"
S=$1
E=${2:-$(python3 -c "
from funcs import FuncIndex
from elf import ELF
import sys
e=ELF('$BIN'); fi=FuncIndex(e); print(hex(fi.end(int('$1',16))))
")}
objdump -d --start-address=$S --stop-address=$E -M intel "$BIN" | sed -n '8,$p'
