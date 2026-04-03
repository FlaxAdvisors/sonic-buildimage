#!/bin/bash
# read_ledup.sh — dump BCM LEDUP DATA_RAM per-port status bytes
# Run on SONiC target. Uses single bcmcmd call, parses output.
#
# Bit layout per DATA_RAM entry:
#   7: Link Up    6: Flow Control   5: Full Duplex
#   4:3: Speed (00=10M, 01=100M, 10=1G, 11=10G+)
#   2: Collision  1: TX activity    0: RX activity

PROCS="${1:-0 1}"
OUTFILE="/tmp/ledup_dump.txt"

for proc in $PROCS; do
    # Single bcmcmd call, dump all DATA_RAM to file inside container
    docker exec syncd bash -c "bcmcmd -t 15 'getreg CMIC_LEDUP${proc}_DATA_RAM' > /tmp/ledup${proc}.txt 2>&1"
    docker cp syncd:/tmp/ledup${proc}.txt /tmp/ledup${proc}.txt 2>/dev/null

    echo "=== LEDUP${proc} DATA_RAM ==="
    printf "%-6s %-6s %s\n" "Entry" "Value" "Flags"

    grep "DATA_RAM" /tmp/ledup${proc}.txt | while IFS= read -r line; do
        idx=$(echo "$line" | grep -oP '\((\d+)\)' | tr -d '()')
        val=$(echo "$line" | grep -oP '=0x[0-9a-fA-F]+' | cut -d= -f2)
        [ -z "$idx" ] || [ -z "$val" ] && continue
        [ "$idx" -gt 31 ] && continue

        dec=$((val))
        flags=""
        [ $((dec & 0x80)) -ne 0 ] && flags="${flags}Link "
        [ $((dec & 0x40)) -ne 0 ] && flags="${flags}FC "
        [ $((dec & 0x20)) -ne 0 ] && flags="${flags}FD "
        spd=$(( (dec >> 3) & 3 ))
        case $spd in
            0) flags="${flags}10M " ;;
            1) flags="${flags}100M " ;;
            2) flags="${flags}1G " ;;
            3) flags="${flags}10G+ " ;;
        esac
        [ $((dec & 0x04)) -ne 0 ] && flags="${flags}Col "
        [ $((dec & 0x02)) -ne 0 ] && flags="${flags}TX "
        [ $((dec & 0x01)) -ne 0 ] && flags="${flags}RX "
        [ $dec -eq 0 ] && flags="(dark)"
        printf "%-6s %-6s %s\n" "[$idx]" "$val" "$flags"
    done
    echo ""
done
