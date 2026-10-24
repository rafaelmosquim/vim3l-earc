# Reading and writing the eARC Capabilities Data Structure

The CDS is the block the TV reads to learn what formats the receiver accepts —
the eARC equivalent of the audio part of an EDID. On this driver it is an
ordinary ALSA mixer control, 256 bytes, **read/write**:

```
amixer -c0 cget "name=eARC_RX CDS"
amixer -c0 cset "name=eARC_RX CDS" "0x01,0x01,...,0x00"   # all 256 bytes
```

No root needed — it is just a mixer control. `tools/earc-cds.sh` builds the
header and length bytes for you and has presets; `tools/earc-cds.sh show`
prints the first 16 bytes of whatever is loaded.

There is no ready-made decoder for it that I could find, but you do not really
need one: past a 3-byte header it is **CTA-861 data blocks**, so the Short Audio
Descriptors are the same ones you would read out of an EDID. CTA-861 table 37,
or edid-decode's source, is the reference.

## Worked example

The default this repo loads, trimmed to the bytes that are not padding:

```
01 01 0e 29 0f 7f 07 15 07 50 57 07 00 83 0f 40
```

```
01 01      eARC capabilities header
0e         payload length, 14 bytes follow
29         CTA data block header: tag 1 (audio) | length 9  -> three 3-byte SADs

0f 7f 07   SAD 1
           0f = 0000 1111  format = bits 6:3 = 1 (LPCM), channels = bits 2:0 = 7 -> 8 ch
           7f = rate bitmask, bits 0..6 = 32 / 44.1 / 48 / 88.2 / 96 / 176.4 / 192 kHz
           07 = word lengths, bits 0..2 = 16 / 20 / 24 bit
15 07 50   SAD 2
           15 -> format 2 (AC-3), 6 ch
           07 -> 32 / 44.1 / 48 kHz
           50 = 80; for a compressed format byte 3 is max bitrate / 8 kbit/s -> 640 kbps
57 07 00   SAD 3
           57 -> format 10 (E-AC-3), 8 ch
           07 -> 32 / 44.1 / 48 kHz
           00 -> reserved for E-AC-3
83 0f 40   CTA data block: tag 4 (speaker allocation) | length 3
           0f = FL/FR + LFE + FC + RL/RR -> 5.1
```

So that block decodes to:

| Advertised | Channels | Rates          | Cap            |
|------------|----------|----------------|----------------|
| LPCM       | 8        | 32–192 kHz     | 16 / 20 / 24 bit |
| AC-3       | 6        | 32/44.1/48 kHz | 640 kbps       |
| E-AC-3     | 8        | 32/44.1/48 kHz | —              |

## Channel counts and format codes

Channels are `bits 2:0 + 1`, so 7 means 8 channels. The format code in bits 6:3
is the CTA-861 audio format code: 1 LPCM, 2 AC-3, 7 DTS-HD, 8 MLP (TrueHD),
10 E-AC-3, 11 DTS-HD, 12 Dolby MAT.

## Writing it, and what that does not buy you

The driver accepts whatever you write. A MAT descriptor — `0x67,0x54,0x00`,
i.e. format code 12, 8 channels, 48/96/192 kHz — was accepted here and the link
stayed up at attended type 2. **That proves nothing about whether the receiver
can decode MAT**; it only proves the capability block is yours to write.

The TV only re-reads the CDS when the link comes up, so a change needs a link
bounce or a TV-side eARC toggle to have any chance of being noticed. And on the
one TV tested here it was not noticed in a way that mattered: a Dolby-only CDS
(no LPCM advertised at all) triggered a full re-discovery and the TV carried on
sending LPCM regardless. See [findings.md](findings.md).

The CDS is not persistent — a reboot or a restart of `earc-cec.service` puts the
default back.
