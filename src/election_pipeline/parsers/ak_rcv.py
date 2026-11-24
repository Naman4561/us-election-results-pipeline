"""Alaska's RCV Detailed Report: one race's ranked-choice tabulation, checked round by round.

The Division of Elections posts one report per race that needed more than a first round
(``RCV-USRep.pdf`` for the U.S. House); a race won by a first-round majority has none.
The report prints, for each round, every candidate's votes and percentage, the
continuing ballots, and the blanks, exhausted ballots and overvotes, then which
candidate is eliminated and where that candidate's ballots went.

Every number is checked against the others: each round's candidates add up to its
continuing ballots, its non-transferable lines to their total, and the two to the same
ballots in every round; the eliminated candidate is the one with the fewest votes, its
ballots all transfer, and the next round is the previous one plus those transfers; and
the elected candidate holds a majority of the continuing ballots in the last round.
"""

import io
import re

from pypdf import PdfReader

MAX_PAGES = 20
PREAMBLE = ("RCV Detailed Report", "State of Alaska", "November 03, 2026", "General Election")
# The tabulation rules the checks below assume: instant runoff, one candidate eliminated
# per round, and a write-in ranking skipped rather than counted.
OPTIONS = {
    "RCV method": "IRV",
    "Elimination type": "Single",
    "Exclude unresolved write-ins": "True",
    "Perform elimination transfer in last round": "True",
}
NON_TRANSFERABLE = ("Blanks", "Exhausted", "Overvotes")
TRANSFER_HEADER = ["Transferred from", "Transferred to", "Ballots", "Fraction", "Votes"]
PAGE_TIME = re.compile(r"\d\d/\d\d/\d{4} \d\d:\d\d:\d\d")
TRANSFER = re.compile(r"Elimination transfer for candidate (.+)\.")
TRANSFERRED = re.compile(r"([\d,]+) ballots have been transferred in the following manner:")


def count(text):
    # Votes print thousands separators; transferred ballot counts do not.
    if not re.fullmatch(r"\d+|\d{1,3}(?:,\d{3})+", text):
        raise ValueError(f"Alaska RCV report count is not a number: {text!r}")
    return int(text.replace(",", ""))


def lines_of(raw, settings):
    """The report's text lines, without the page header each page repeats."""
    if not raw.startswith(b"%PDF-"):
        raise ValueError("Alaska RCV report is not a PDF")
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError("Alaska RCV report is encrypted or has an unexpected page count")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if len(text) > settings.max_uncompressed_bytes:
        raise ValueError("Alaska RCV report text is too large")
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    kept, index, printed = [], 0, set()
    while index < len(lines):
        # "11/23/2026 11:07:13", "Page:", "2", "/", "3"
        if PAGE_TIME.fullmatch(lines[index]) and lines[index + 1 : index + 2] == ["Page:"]:
            printed.add(lines[index])
            index += 5
            continue
        kept.append(lines[index])
        index += 1
    if len(printed) != 1:
        raise ValueError("Alaska RCV report pages were printed at different times")
    return kept


def value_after(lines, label):
    if lines.count(label) != 1:
        raise ValueError(f"Alaska RCV report lacks one {label!r}")
    return lines[lines.index(label) + 1]


def read(raw, title, candidates, settings):
    """{"official", "tabulated_at", "ballots", "rounds", "elected"} for the race ``title``,
    whose candidates are ``candidates`` (the reviewed ballot names)."""
    lines = lines_of(raw, settings)
    if tuple(lines[:4]) != PREAMBLE or lines[4] != title:
        raise ValueError(f"Alaska RCV report is not the 2026 general election's {title}")
    if lines[5] not in ("Official results", "Unofficial results"):
        raise ValueError("Alaska RCV report does not state whether it is official")
    if value_after(lines, "Tabulation status:") != "All Positions Filled":
        raise ValueError("Alaska RCV tabulation is not complete")
    for option, expected in OPTIONS.items():
        if value_after(lines, option) != expected:
            raise ValueError(f"Alaska RCV tabulation option changed: {option}")
    starts = [
        i
        for i in range(len(lines) - 4)
        if lines[i] == "Round" and lines[i + 2 : i + 5] == ["Candidate", "Votes", "Percentage"]
    ]
    if not starts:
        raise ValueError("Alaska RCV report has no rounds")
    rounds = []
    for number, (start, end) in enumerate(zip(starts, [*starts[1:], len(lines)]), 1):
        if lines[start + 1] != str(number):
            raise ValueError("Alaska RCV rounds are not numbered in order")
        rounds.append(round_of(lines[start + 5 : end], candidates))
    return {
        "official": lines[5] == "Official results",
        "tabulated_at": value_after(lines, "Tabulation time:"),
        **checked(rounds, candidates),
    }


def round_of(lines, candidates):
    """One round's printed votes, its non-transferable lines and what happened after it."""
    votes, percentages, index = {}, {}, 0
    while lines[index] != "Continuing Ballots Total":
        name, value, share = lines[index : index + 3]
        if name in votes or name not in candidates or not re.fullmatch(r"\d+\.\d\d%", share):
            raise ValueError(f"Alaska RCV round lists an unexpected candidate line: {name!r}")
        votes[name], percentages[name] = count(value), share
        index += 3
    labels = ["Continuing Ballots Total", *NON_TRANSFERABLE, "Non Transferable Total"]
    if lines[index : index + 10 : 2] != labels:
        raise ValueError("Alaska RCV round totals are not in the expected order")
    totals = {label: count(lines[index + 1 + 2 * n]) for n, label in enumerate(labels)}
    index += 10
    outcomes, transfers = {}, {}
    while index < len(lines):
        if lines[index + 1 : index + 2] == ["is"]:
            name, verdict = lines[index], lines[index + 2]
            if (
                name not in candidates
                or name in outcomes
                or verdict
                not in (
                    "eliminated",
                    "elected",
                )
            ):
                raise ValueError(f"Alaska RCV report states an unexpected outcome for {name!r}")
            if lines[index + 3] != "because":
                raise ValueError("Alaska RCV outcome gives no reason")
            outcomes[name] = verdict
            index += 4
            while not lines[index].endswith("."):
                index += 1
            index += 1
        elif match := TRANSFER.fullmatch(lines[index]):
            source = match.group(1)
            moved = TRANSFERRED.fullmatch(lines[index + 1])
            if source in transfers or not moved or lines[index + 2 : index + 7] != TRANSFER_HEADER:
                raise ValueError("Alaska RCV transfer is not in the expected layout")
            given, index = {}, index + 7
            while index + 4 < len(lines) and lines[index] == source:
                target, ballots, fraction, value = lines[index + 1 : index + 5]
                if target in given or fraction != "1" or count(ballots) != count(value):
                    raise ValueError("Alaska RCV transfer line is not whole ballots")
                given[target] = count(value)
                index += 5
            if sum(given.values()) != count(moved.group(1)):
                raise ValueError("Alaska RCV transfer lines do not add up to its ballots")
            transfers[source] = given
        else:
            raise ValueError(f"Alaska RCV report has an unexpected line: {lines[index]!r}")
    return {
        "votes": votes,
        "percentages": percentages,
        "totals": totals,
        "outcomes": outcomes,
        "transfers": transfers,
    }


def checked(rounds, candidates):
    """Check the rounds against each other; return the ballots, rounds and winner."""
    ballots, out = None, set()
    for number, current in enumerate(rounds, 1):
        votes, totals = current["votes"], current["totals"]
        last = number == len(rounds)
        if set(votes) != set(candidates):
            raise ValueError("Alaska RCV round does not list every candidate")
        if sum(votes.values()) != totals["Continuing Ballots Total"]:
            raise ValueError("Alaska RCV candidates do not add up to the continuing ballots")
        if sum(totals[n] for n in NON_TRANSFERABLE) != totals["Non Transferable Total"]:
            raise ValueError("Alaska RCV non-transferable lines do not add up")
        cast = totals["Continuing Ballots Total"] + totals["Non Transferable Total"]
        if ballots is None:
            ballots = cast
        elif cast != ballots:
            raise ValueError("Alaska RCV rounds do not hold the same ballots")
        for name, value in votes.items():
            share = 100 * value / totals["Continuing Ballots Total"]
            if abs(share - float(current["percentages"][name][:-1])) > 0.005 + 1e-9:
                raise ValueError("Alaska RCV percentage differs from its votes")
            if name in out and value:
                raise ValueError("Alaska RCV round counts votes for an eliminated candidate")
        standing = {n: v for n, v in votes.items() if n not in out}
        outcomes = current["outcomes"]
        if not last:
            # One candidate is eliminated, the one with the fewest votes (a tie for fewest
            # is resolved by law, outside the report: it is refused here).
            fewest = min(standing.values())
            losers = [n for n, v in standing.items() if v == fewest]
            if (
                list(outcomes.items()) != [(losers[0], "eliminated")]
                or len(losers) != 1
                or set(current["transfers"]) != set(outcomes)
            ):
                raise ValueError("Alaska RCV round does not eliminate its last candidate alone")
            loser = losers[0]
            given = current["transfers"][loser]
            if sum(given.values()) != votes[loser] or not set(given) <= (
                set(standing) - {loser}
            ) | {"Exhausted", "Overvotes"}:
                raise ValueError("Alaska RCV transfer does not move every eliminated ballot")
            following = rounds[number]
            expected = {
                n: 0 if n in out or n == loser else v + given.get(n, 0) for n, v in votes.items()
            }
            if following["votes"] != expected:
                raise ValueError("Alaska RCV round is not the previous round plus its transfer")
            for name in ("Exhausted", "Overvotes"):
                if following["totals"][name] != totals[name] + given.get(name, 0):
                    raise ValueError(f"Alaska RCV {name.lower()} do not carry the transfer")
            if following["totals"]["Blanks"] != totals["Blanks"]:
                raise ValueError("Alaska RCV blanks change between rounds")
            out.add(loser)
        else:
            elected = [n for n, verdict in outcomes.items() if verdict == "elected"]
            if (
                len(elected) != 1
                or current["transfers"]
                or set(outcomes) != set(standing)
                or 2 * votes[elected[0]] <= totals["Continuing Ballots Total"]
            ):
                raise ValueError("Alaska RCV last round does not elect a majority winner")
    return {"ballots": ballots, "rounds": rounds, "elected": elected[0]}


def rows(contest_id, report):
    """The tabulation in the shared layout of ``rcv_rounds.csv``: per round, each candidate
    still in it (``continuing``, ``eliminated`` after it, ``elected`` or ``defeated`` in
    the last), then its blanks, exhausted ballots and overvotes."""
    found, out = [], set()
    for number, current in enumerate(report["rounds"], 1):
        last = number == len(report["rounds"])
        for name, votes in sorted(current["votes"].items()):
            if name in out:
                continue
            verdict = current["outcomes"].get(name)
            outcome = (
                ("elected" if verdict == "elected" else "defeated")
                if last
                else ("eliminated" if verdict == "eliminated" else "continuing")
            )
            found.append(
                {
                    "contest_id": contest_id,
                    "round": number,
                    "kind": "candidate",
                    "choice": name,
                    "votes": votes,
                    "outcome": outcome,
                }
            )
            if outcome == "eliminated":
                out.add(name)
        for kind in NON_TRANSFERABLE:
            found.append(
                {
                    "contest_id": contest_id,
                    "round": number,
                    "kind": kind.lower(),
                    "choice": "",
                    "votes": current["totals"][kind],
                    "outcome": "",
                }
            )
    return found
