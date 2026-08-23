# `data/incoming/`

`transcript.md` is the canonical input — every module (M2, M3) reads this file, and its fingerprint gates their offline sample output.

`transcript.vtt` is the platform-export twin: a real webinar platform (Zoom, ON24, GoTo) hands you a WebVTT export, not a hand-formatted markdown transcript, so this proves the pipeline's input contract survives that shape too. Regenerate it from `transcript.md` with `python3 data/incoming/tools/md_to_vtt.py` — never hand-edit `transcript.vtt` directly, or it will drift from the canonical source.
