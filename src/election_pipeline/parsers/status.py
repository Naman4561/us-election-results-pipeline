"""Whether a state's own results page labels its results unofficial or official.

Each state's label is a phrase with ``{}`` where the page prints "Unofficial". Its
official form is that phrase with "Official": the expected wording, not yet seen on any
of these sites this election. A page
showing neither form, or both, is refused, so a certified page worded otherwise stops
the parse and alerts rather than passing as unofficial.
"""

import re


def results_status(body, phrase, state):
    """True when ``body`` shows only the official form of ``phrase``, False when only the
    unofficial one."""
    words = " ".join(body.split())
    seen = {
        official
        for official, label in ((False, "Unofficial"), (True, "Official"))
        if re.search(r"(?<![A-Za-z])" + re.escape(phrase.format(label)), words)
    }
    if len(seen) != 1:
        raise ValueError(f"{state} page does not label its results either unofficial or official")
    return seen.pop()
