#!/bin/bash
# Set the eARC RX Capabilities Data Structure (what the TV reads to learn what
# formats we accept). The TV only re-reads the CDS when the eARC link comes up,
# so a change needs a link bounce (fake_plug 0->1) or a TV-side eARC toggle.
#
#   dolby   AC-3 + E-AC-3 only  (no LPCM -> TV must encode its PCM to Dolby)
#   eac3    E-AC-3 only
#   ac3     AC-3 only
#   default LPCM 7.1 + AC-3 + E-AC-3  (what earc-cec-daemon.py sets at boot)
#   mat     default + Dolby MAT (CTA format 12) -- needed for TrueHD/Atmos-MAT;
#           the TV only honours it after it re-reads the CDS, i.e. a TV power cycle
#   show    print the CDS currently loaded
set -u
SAD_LPCM="0x0f,0x7f,0x07"
SAD_AC3="0x15,0x07,0x50"
SAD_EAC3="0x57,0x07,0x00"
SAD_MAT="0x67,0x54,0x00"   # format 12, 8ch, 48/96/192 kHz
SPK="0x83,0x0f,0x40,0x00"

case "${1-show}" in
  default) SADS="$SAD_LPCM,$SAD_AC3,$SAD_EAC3" ;;
  mat)     SADS="$SAD_LPCM,$SAD_AC3,$SAD_EAC3,$SAD_MAT" ;;
  dolby)   SADS="$SAD_AC3,$SAD_EAC3" ;;
  eac3)    SADS="$SAD_EAC3" ;;
  ac3)     SADS="$SAD_AC3" ;;
  show)    amixer -c0 cget "name=eARC_RX CDS" | sed -n 's/^  : values=//p' | cut -d, -f1-16; exit 0 ;;
  *) echo "usage: $0 {default|mat|dolby|eac3|ac3|show}" >&2; exit 2 ;;
esac

n=$(( $(tr -cd , <<<"$SADS" | wc -c) + 1 ))        # number of SAD bytes groups
adb_len=$(( n ))                                    # bytes in the Audio Data Block
hdr=$(printf '0x%02x' $(( 0x20 | adb_len )))        # CTA tag 1 (audio) | length
payload=$(( adb_len + 1 + 4 ))                      # ADB header+SADs + speaker block
CDS="0x01,0x01,$(printf '0x%02x' $payload),$hdr,$SADS,$SPK"
# pad to the control's 256 bytes
cur=$(( $(tr -cd , <<<"$CDS" | wc -c) + 1 ))
for ((i=cur; i<256; i++)); do CDS="$CDS,0x00"; done
amixer -q -c0 cset "name=eARC_RX CDS" "$CDS" || exit 1
echo "CDS set to '$1':"
amixer -c0 cget "name=eARC_RX CDS" | sed -n 's/^  : values=//p' | cut -d, -f1-16
