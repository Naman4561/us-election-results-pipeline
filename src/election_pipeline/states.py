"""Every covered state: its races, vote methods and parser, shared by jobs and the dashboard."""

from dataclasses import dataclass
from importlib import import_module


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
    vote_basis: str = "reported_votes"
    collection_backend: str = ""
    county_feed_status: bool = False
    # The state hides small county counts for voter privacy (printed as "*"). Its county
    # rows may then add up to less than an official total, never more.
    county_privacy_masking: bool = False
    # Parser module under election_pipeline.parsers; defaults to the slug. Oregon's
    # slug "or" is a Python keyword, so its parser module has a different name.
    parser_module: str = ""
    # Shown under a race when the state's vote-method breakdown needs explaining.
    method_note: str = ""
    # What the state's local results units are: "county", or "town" where a state
    # reports by town and its map draws towns (Vermont).
    local_unit: str = "county"

    @property
    def slug(self):
        """Lowercase code naming the state's parser module and map file."""
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
    def parser(self):
        return import_module(f"election_pipeline.parsers.{self.parser_module or self.slug}")

    @property
    def datasets(self):
        """Election results, plus any ancillary dataset the parser stores itself."""
        return ("election_results", *getattr(self.parser, "DATASETS", {}))


STATES = {
    "MS": State(
        "MS",
        "Mississippi",
        "ms_2026_general_all",
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
        "nc_2026_general_all",
        tuple(range(1, 15)),
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
        method_note=(
            "Kentucky combines absentee votes. Separate early, mail and provisional counts "
            "are unavailable. Precinct reporting status is not a percentage of votes counted."
        ),
    ),
    "MN": State(
        "MN",
        "Minnesota",
        "mn_2026_general_all",
        tuple(range(1, 9)),
        ("total_votes",),
        governor=True,
        source_page="https://electionresults.sos.mn.gov/",
    ),
    "NE": State(
        "NE",
        "Nebraska",
        "ne_2026_general_all",
        (1, 2, 3),
        ("total_votes",),
        governor=True,
        county_results=False,
        source_page="https://electionresults.nebraska.gov/",
    ),
    "TX": State(
        "TX",
        "Texas",
        "tx_2026_general_all",
        tuple(range(1, 39)),
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
        source_page="https://www.enr-scvotes.org/SC/127300/",
        collection_backend="clarity",
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
    "OK": State(
        "OK",
        "Oklahoma",
        "ok_2026_general",
        tuple(range(1, 6)),
        ("total_votes", "election_day", "early_voting", "absentee_by_mail"),
        governor=True,
        source_page="https://results.okelections.gov/",
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
    # Indiana elects no senator or governor in 2026; "in" is a Python keyword.
    "IN": State(
        "IN",
        "Indiana",
        "in_2026_general",
        tuple(range(1, 10)),
        ("total_votes",),
        senate=False,
        source_page="https://enr.indianavoters.in.gov/site/index.html",
        collection_backend="indiana",
        parser_module="indiana",
    ),
    "AR": State(
        "AR",
        "Arkansas",
        "ar_2026_general",
        (1, 2, 3, 4),
        ("total_votes",),
        governor=True,
        source_page="https://arkansas.tally-enr.com/",
    ),
    # North Dakota elects no senator or governor in 2026.
    "ND": State(
        "ND",
        "North Dakota",
        "nd_2026_general",
        (0,),
        ("total_votes",),
        senate=False,
        source_page="https://resultsnd.sos.nd.gov/",
    ),
    # Louisiana elects no governor in 2026; counties are parishes.
    "LA": State(
        "LA",
        "Louisiana",
        "la_2026_general",
        tuple(range(1, 7)),
        ("total_votes",),
        source_page="https://voterportal.sos.la.gov/graphical",
        method_note=(
            "Louisiana's U.S. House races are open to every candidate; one without a "
            "majority goes to a December runoff between the top two. These are the "
            "November 3 results."
        ),
    ),
    # Delaware elects no governor in 2026.
    "DE": State(
        "DE",
        "Delaware",
        "de_2026_general",
        (0,),
        ("total_votes",),
        source_page="https://elections.delaware.gov/results/enr/GE2026.html",
    ),
    # Vermont elects no senator in 2026; its map and results are by town.
    "VT": State(
        "VT",
        "Vermont",
        "vt_2026_general",
        (0,),
        ("total_votes",),
        senate=False,
        governor=True,
        source_page="https://electionresults.vermont.gov/",
        collection_backend="vermont",
        local_unit="town",
    ),
    # Rhode Island reports by city and town, not county.
    "RI": State(
        "RI",
        "Rhode Island",
        "ri_2026_general",
        (1, 2),
        ("total_votes", "election_day", "early_voting", "absentee_by_mail"),
        governor=True,
        county_results=False,
        source_page="https://electionresults.ri.gov/results/public/rhodeisland/elections/2026StatewideGeneralElection",
        collection_backend="enhanced_voting",
    ),
    # Wyoming has no election-night source collected here; it is added from the State
    # Canvassing Board's certified results, imported as a manual source.
    "WY": State(
        "WY",
        "Wyoming",
        "wy_2026_general",
        (0,),
        ("total_votes",),
        governor=True,
        source_page="https://sos.wyo.gov/Elections/Docs/2026/2026GeneralResults.aspx",
    ),
    # Utah elects no senator or governor in 2026.
    "UT": State(
        "UT",
        "Utah",
        "ut_2026_general",
        (1, 2, 3, 4),
        ("total_votes",),
        senate=False,
        source_page="https://electionresults.utah.gov/results/public/utah/elections/General11032026",
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
    # West Virginia elects no governor in 2026.
    "WV": State(
        "WV",
        "West Virginia",
        "wv_2026_general",
        (1, 2),
        ("total_votes",),
        source_page="https://results.enr.clarityelections.com/WV/127496/",
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
        parser_module="oregon",
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
        collection_backend="gated_files",
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
        collection_backend="gated_files",
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
