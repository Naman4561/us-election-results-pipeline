"""State adapters and the stable result contract shared by jobs and the dashboard."""

from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True)
class LoadContract:
    table: str
    filename: str
    fields: tuple[str, ...]
    files: tuple[str, ...]
    format: str
    auxiliary: tuple = ()


@dataclass(frozen=True)
class State:
    code: str
    name: str
    source_id: str
    house: tuple[int, ...]
    methods: tuple[str, ...]
    senate: bool = True
    governor: bool = False
    county_results: bool = True
    source_page: str = ""
    bundle_resolver: str = ""
    vote_basis: str = "reported_votes"
    collection_backend: str = ""
    county_feed_status: bool = False
    # The state hides small county counts for voter privacy (printed as "*"). Its county
    # rows may then add up to less than an official total, never more.
    county_privacy_masking: bool = False

    @property
    def schema(self):
        return self.code.lower()

    @property
    def races(self):
        return {
            **({f"{self.code}-SEN": "U.S. Senate"} if self.senate else {}),
            **({f"{self.code}-GOV": "Governor"} if self.governor else {}),
            **{
                (
                    identity := self.code + ("-AL" if n == 0 else f"-{n:02}")
                ): f"U.S. House · {identity}"
                for n in self.house
            },
        }

    @property
    def available_races(self):
        """All supported offices; an explicit source contract selects its coverage."""
        seats = {"NC": 14, "MN": 8, "NE": 3, "TX": 38}.get(self.code)
        if seats is None:
            return self.races
        return {
            **{f"{self.code}-SEN": "U.S. Senate"},
            **({f"{self.code}-GOV": "Governor"} if self.code != "NC" else {}),
            **{
                f"{self.code}-{n:02}": f"U.S. House · {self.code}-{n:02}"
                for n in range(1, seats + 1)
            },
        }

    def selected_races(self, source):
        if source.state != self.code:
            raise ValueError("Race contract belongs to another state")
        if not source.contests:
            return self.races
        if not set(source.contests) <= self.available_races.keys():
            raise ValueError("Source selects an unsupported contest")
        return {identity: self.available_races[identity] for identity in source.contests}

    @property
    def parser(self):
        return import_module(f"election_pipeline.parsers.{self.schema}")

    def contract(self, dataset):
        return self.parser.load_contract(dataset)


STATES = {
    "MS": State(
        "MS",
        "Mississippi",
        "ms_2026_general",
        (),
        ("total_votes",),
        source_page="https://www.sos.ms.gov/MSCountyElectionInfo/",
        vote_basis="covered_counties_named_candidates",
        collection_backend="county_files",
        county_feed_status=True,
    ),
    "AK": State(
        "AK",
        "Alaska",
        "ak_2026_general",
        (0,),
        ("total_votes",),
        governor=True,
        county_results=False,
        source_page="https://www.elections.alaska.gov/election-results/e/?id=26genr",
        vote_basis="first_choice_listed_candidates",
    ),
    "NC": State(
        "NC",
        "North Carolina",
        "nc_2026_general",
        (1, 3, 4, 11),
        ("total_votes", "election_day", "early_voting", "absentee_by_mail", "provisional"),
        source_page="https://er.ncsbe.gov/",
    ),
    "KY": State(
        "KY",
        "Kentucky",
        "ky_2026_general",
        tuple(range(1, 7)),
        ("total_votes", "election_day", "absentee_combined"),
        source_page="https://vrsws.sos.ky.gov/liveresults/",
    ),
    "MN": State(
        "MN",
        "Minnesota",
        "mn_2026_general",
        (1,),
        ("total_votes",),
        source_page="https://electionresults.sos.mn.gov/",
    ),
    "NE": State(
        "NE",
        "Nebraska",
        "ne_2026_general",
        (),
        ("total_votes",),
        governor=True,
        county_results=False,
        source_page="https://electionresults.nebraska.gov/",
    ),
    "TX": State(
        "TX",
        "Texas",
        "tx_2026_general",
        (15, 23, 28, 34, 35),
        ("total_votes",),
        governor=True,
        county_results=False,
        source_page="https://goelect.txelections.civixapps.com/ivis-enr-ui/races",
    ),
    "GA": State(
        "GA",
        "Georgia",
        "ga_2026_general",
        tuple(range(1, 15)),
        ("total_votes",),
        governor=True,
        county_results=False,
        source_page="https://results.sos.ga.gov/results/public/Georgia/elections/11032026GeneralAndSpecialElection",
    ),
    "SC": State(
        "SC",
        "South Carolina",
        "sc_2026_general",
        tuple(range(1, 8)),
        ("total_votes",),
        governor=True,
        source_page="https://scvotes.gov/2026-general-election-results/",
        bundle_resolver="sc",
    ),
    "KS": State(
        "KS",
        "Kansas",
        "ks_2026_general",
        tuple(range(1, 5)),
        ("total_votes",),
        governor=True,
        source_page="https://ent.sos.ks.gov/kssos_ent.html",
    ),
    "OH": State(
        "OH",
        "Ohio",
        "oh_2026_general",
        tuple(range(1, 16)),
        ("total_votes",),
        governor=True,
        source_page="https://liveresults.ohiosos.gov/",
    ),
    "SD": State(
        "SD",
        "South Dakota",
        "sd_2026_general",
        (0,),
        ("total_votes",),
        governor=True,
        source_page="https://electionresults.sd.gov/",
        collection_backend="enr_export",
    ),
    "NM": State(
        "NM",
        "New Mexico",
        "nm_2026_general",
        (1, 2, 3),
        ("total_votes",),
        governor=True,
        source_page="https://electionresults.sos.nm.gov/",
        collection_backend="enr_export",
        county_privacy_masking=True,
    ),
    # District 10 was uncontested, so Florida omitted it from the ballot and its results.
    "FL": State(
        "FL",
        "Florida",
        "fl_2026_general",
        tuple(n for n in range(1, 29) if n != 10),
        ("total_votes",),
        governor=True,
        source_page="https://floridaelectionwatch.gov/",
    ),
    "VA": State(
        "VA",
        "Virginia",
        "va_2026_general",
        tuple(range(1, 12)),
        (
            "total_votes",
            "early_voting",
            "election_day",
            "absentee_by_mail",
            "provisional",
            "post_election",
        ),
        source_page=(
            "https://enr.elections.virginia.gov/results/public/virginia/elections/"
            "2026-November-General"
        ),
        collection_backend="enhanced_voting",
    ),
    "ID": State(
        "ID",
        "Idaho",
        "id_2026_general",
        (1, 2),
        ("total_votes",),
        governor=True,
        source_page="https://results.voteidaho.gov/results/public/id/elections/nov2026",
        collection_backend="enhanced_voting",
    ),
    # Washington elects no senator or governor in 2026.
    "WA": State(
        "WA",
        "Washington",
        "wa_2026_general",
        tuple(range(1, 11)),
        ("total_votes",),
        senate=False,
        source_page="https://results.votewa.gov/results/public/washington/elections/20261103",
        collection_backend="enhanced_voting",
    ),
    "CO": State(
        "CO",
        "Colorado",
        "co_2026_general",
        tuple(range(1, 9)),
        ("total_votes",),
        governor=True,
        source_page="https://results.enr.clarityelections.com/CO/127407/",
        collection_backend="clarity",
    ),
    "IA": State(
        "IA",
        "Iowa",
        "ia_2026_general",
        (1, 2, 3, 4),
        ("total_votes", "election_day", "absentee_combined"),
        governor=True,
        source_page="https://results.enr.clarityelections.com/IA/127077/",
        collection_backend="clarity",
    ),
    "OR": State(
        "OR",
        "Oregon",
        "or_2026_general",
        tuple(range(1, 7)),
        ("total_votes",),
        governor=True,
        source_page="https://results.oregonvotes.gov/",
        collection_backend="enr_export",
    ),
    # California elects no senator in 2026. Its results API is collected statewide only.
    "CA": State(
        "CA",
        "California",
        "ca_2026_general",
        tuple(range(1, 53)),
        ("total_votes",),
        senate=False,
        governor=True,
        county_results=False,
        source_page="https://electionresults.sos.ca.gov/",
    ),
    # Maryland elects no senator in 2026.
    "MD": State(
        "MD",
        "Maryland",
        "md_2026_general",
        tuple(range(1, 9)),
        ("total_votes",),
        senate=False,
        governor=True,
        source_page="https://elections.maryland.gov/elections/2026/general_results/",
    ),
    # Hawaii elects no senator in 2026; its county detail is only published as PDFs.
    "HI": State(
        "HI",
        "Hawaii",
        "hi_2026_general",
        (1, 2),
        ("total_votes", "mail", "in_person"),
        senate=False,
        governor=True,
        county_results=False,
        source_page="https://elections.hawaii.gov/election-results/",
    ),
}


def state_adapter(code):
    if code not in STATES:
        raise ValueError(f"No state adapter registered for {code}")
    return STATES[code]


def parser_for(source):
    adapter = state_adapter(source.state)
    return adapter.parser.parse


def publication_lock(conn, state):
    # A shared maintenance lock allows state jobs to overlap while backups stay exclusive.
    if not conn.execute("SELECT pg_try_advisory_lock_shared(20261103, 1) AS ok").fetchone()["ok"]:
        raise ValueError("Backup is already running; retry this state later")
    key = int.from_bytes(state.encode("ascii"), "big")
    if not conn.execute("SELECT pg_try_advisory_lock(20261104, %s) AS ok", (key,)).fetchone()["ok"]:
        raise ValueError(f"Another {state} publication or restore is running")
