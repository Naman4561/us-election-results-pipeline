"""The analysis workspace: python -m analysis open

A marimo notebook. SQL cells run in DuckDB, which attaches the local analysis database
read-only as ``elections`` (so ``elections.serving.county_results``); a SQL cell's result
is a dataframe the next Python cell can use, and the reverse. Add cells for your own work
and save; this file is the starting point, not a fixed report.
"""

import marimo

__generated_with = "0.25.2"
app = marimo.App(width="full", app_title="Election analysis")


@app.cell
def _():
    import duckdb
    import marimo as mo

    from analysis import local

    db = local.settings()
    con = duckdb.connect()
    con.execute("INSTALL postgres; LOAD postgres;")
    con.execute(
        "ATTACH 'host={host} port={port} dbname={dbname} user={user} password={password}'"
        " AS elections (TYPE postgres, READ_ONLY)".format(**db)
    )
    # The current results with each county's FIPS code; Governor, Senate and House only.
    con.execute("""
        CREATE OR REPLACE TEMP VIEW county_results AS
        SELECT p.state, r.contest_id,
               CASE WHEN r.contest_id LIKE '%-SEN' THEN 'Senate'
                    WHEN r.contest_id LIKE '%-GOV' THEN 'Governor'
                    ELSE 'House' END AS office,
               r.county, a.fips, r.choice, r.choice_party AS party, r.total_votes,
               p.status, p.published_at
          FROM elections.ref.current_publications p
          JOIN elections.serving.county_results r USING (publication_id)
          LEFT JOIN elections.ref.county_aliases a ON a.state = p.state AND a.county = r.county
    """)
    con.execute("""
        CREATE OR REPLACE TEMP VIEW contest_results AS
        SELECT p.state, r.contest_id,
               CASE WHEN r.contest_id LIKE '%-SEN' THEN 'Senate'
                    WHEN r.contest_id LIKE '%-GOV' THEN 'Governor'
                    ELSE 'House' END AS office,
               r.choice, r.choice_party AS party, r.total_votes, r.vote_share,
               p.status, p.published_at
          FROM elections.ref.current_publications p
          JOIN elections.serving.contest_results r USING (publication_id)
    """)
    # Each Senate and House candidate's FEC money (newest download), beside the result.
    # Outside spending counts only reviewed rows: amended filings replaced, repeated reports
    # once, junk filers excluded (finance.independent_expenditures_reviewed says why).
    con.execute("""
        CREATE OR REPLACE TEMP VIEW candidate_finance AS
        WITH totals AS (
            SELECT * FROM elections.finance.fec_candidate_totals
             WHERE dataset_id = (SELECT dataset_id FROM elections.ref.datasets
                                  WHERE name = 'fec_totals_2026'
                                  ORDER BY downloaded_at DESC LIMIT 1)
        ), outside AS (
            SELECT cand_id,
                   sum(amount) FILTER (WHERE support_oppose = 'S') AS outside_for,
                   sum(amount) FILTER (WHERE support_oppose = 'O') AS outside_against
              FROM elections.finance.independent_expenditures_reviewed
             WHERE counted
             GROUP BY cand_id
        )
        SELECT r.state, r.office, r.contest_id, r.choice, r.party, r.total_votes,
               r.vote_share, i.external_id AS fec_id, t.coverage_end,
               t.receipts, t.disbursements, t.cash_on_hand_end, t.individual_contributions,
               t.candidate_contributions + t.candidate_loans AS self_funding,
               o.outside_for, o.outside_against
          FROM contest_results r
          JOIN elections.ref.candidates c USING (contest_id, choice)
          LEFT JOIN elections.ref.candidate_ids i
                 ON i.candidate_key = c.candidate_key AND i.system = 'fec'
          LEFT JOIN totals t ON t.cand_id = i.external_id
          LEFT JOIN outside o ON o.cand_id = i.external_id
         WHERE r.office IN ('Senate', 'House')
    """)
    return con, mo


@app.cell
def _(con, mo):
    restored = con.execute(
        "SELECT backup_id, backup_finished_at, restore_checked, restored_at"
        " FROM elections.ref.restores ORDER BY restored_at DESC LIMIT 1"
    ).fetchone()
    datasets = con.execute(
        "SELECT name, vintage, license, downloaded_at, source_url FROM elections.ref.datasets"
        " ORDER BY downloaded_at"
    ).df()
    mo.vstack([
        mo.md(
            "# Election analysis\n\n"
            f"Data: backup **{restored[0]}** (finished {restored[1]:%b %d %H:%M} UTC; "
            f"restore-checked on the server: {'yes' if restored[2] else 'no'}), restored "
            f"{restored[3]:%b %d %H:%M}.\n\n"
            "**Limits to carry into any finding:** results are unofficial unless `status` "
            "is `official`; Mississippi covers some counties only; Vermont reports by town; "
            "seven states publish statewide totals only (AK, CA, GA, HI, NE, RI, TX); vote "
            "methods are published by NC, VA, OK, IA, RI and HI only (Iowa's absentee "
            "combines mail and early voting)."
        ),
        mo.md("**Outside data loaded**"),
        mo.ui.table(datasets, selection=None),
    ])  # fmt: skip
    return


@app.cell
def _(con, mo):
    tables = (
        con.execute(
            "SELECT table_schema || '.' || table_name AS name FROM information_schema.tables"
            " WHERE table_catalog = 'elections' AND table_schema NOT LIKE 'p\\_%' ESCAPE '\\'"
            " ORDER BY 1"
        )
        .df()["name"]
        .tolist()
    )
    choice = mo.ui.dropdown(
        ["county_results", "contest_results", *tables],
        value="contest_results",
        label="Browse a table",
    )
    limit = mo.ui.number(1_000, 1_000_000, step=1_000, value=100_000, label="Rows")
    mo.hstack([choice, limit], justify="start")
    return choice, limit


@app.cell
def _(choice, con, limit, mo):
    source = choice.value if "." not in choice.value else "elections." + choice.value
    frame = con.execute(f"SELECT * FROM {source} LIMIT {int(limit.value)}").df()
    # Filter, sort, group and pick columns without code; "Python code" shows the code.
    browser = mo.ui.dataframe(frame)
    browser
    return (browser,)


@app.cell
def _(mo):
    mo.md("""
    ## Your queries

    SQL cells below run in DuckDB. `county_results` and `contest_results` are the current
    results (with FIPS codes); everything else is under `elections.<schema>.<table>`.
    Name a cell's result (the field above its SQL) to use it from Python.
    """)
    return


@app.cell
def _(con, mo):
    statewide = mo.sql(
        """
        -- Each race's leading two candidates and the margin between them.
        WITH ranked AS (
            SELECT *, row_number() OVER (PARTITION BY contest_id ORDER BY total_votes DESC) AS n
              FROM contest_results
        )
        SELECT a.state, a.office, a.contest_id,
               a.choice AS leader, a.party AS leader_party,
               b.choice AS second, b.party AS second_party,
               a.total_votes - b.total_votes AS margin_votes,
               round(100 * (a.vote_share - b.vote_share), 2) AS margin_points,
               a.status
          FROM ranked a JOIN ranked b USING (contest_id)
         WHERE a.n = 1 AND b.n = 2
         ORDER BY margin_points
        """,
        engine=con,
    )
    return (statewide,)


@app.cell
def _(mo, statewide):
    close = statewide[statewide["margin_points"] < 2]
    mo.md(
        f"**{len(close)} races** are within 2 points; the closest is {close.iloc[0]['contest_id']}."
    )
    return


@app.cell
def _(con, mo):
    methods = mo.sql(
        """
        -- How each candidate's votes split by vote method (the six states that publish it).
        SELECT p.state, m.contest_id, m.choice, m.method, m.votes,
               round(100 * m.votes / sum(m.votes) OVER (PARTITION BY m.contest_id, m.choice), 1)
                   AS share_of_candidate
          FROM elections.ref.current_publications p
          JOIN elections.serving.contest_method_results m USING (publication_id)
         ORDER BY m.contest_id, m.choice, m.method
        """,
        engine=con,
    )
    return (methods,)


@app.cell
def _(con, mo):
    money = mo.sql(
        """
        -- Money beside the result, Senate races: candidate spending and outside spending.
        SELECT contest_id, choice, party, round(100 * vote_share, 1) AS pct,
               round(disbursements / 1e6, 2) AS spent_m,
               round(coalesce(outside_for, 0) / 1e6, 2) AS outside_for_m,
               round(coalesce(outside_against, 0) / 1e6, 2) AS outside_against_m,
               coverage_end
          FROM candidate_finance
         WHERE office = 'Senate' AND vote_share >= 0.05
         ORDER BY contest_id, vote_share DESC
        """,
        engine=con,
    )
    return (money,)


if __name__ == "__main__":
    app.run()
