#!/usr/bin/env python3
"""
pandora_receiver_v17.py = v16 with the re-ordered page (player_v17.html). Nothing else changes.

    python pandora_receiver_v17.py

Keep pandora_receiver_v16.py (and v11 to v15, wiki_v14.py and the helper files) in the same folder. Needs player_v17.html.
Open http://127.0.0.1:8765/player
"""
import os
from http.server import ThreadingHTTPServer

import pandora_receiver_v11 as base
import pandora_receiver_v13 as v13
import pandora_receiver_v16 as v16

PAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "player_v17.html")


def main():
    if not os.path.exists(PAGE_FILE):
        raise SystemExit(f"player_v17.html is missing (looked for {PAGE_FILE}). Put it in the same folder as this file.")
    v16.PAGE_FILE = PAGE_FILE
    # v16.main() sets v13.PAGE_FILE from its own PAGE_FILE, so point it at the new page first
    v16.main()


if __name__ == "__main__":
    main()
