"""
DataDawn MCP Server

Exposes the 990 nonprofit and OpenRegs government databases via the
Model Context Protocol, allowing Claude and other MCP-compatible AI
agents to query them directly through the Datasette JSON API.

Usage:
    python server.py                    # stdio transport (default, for Claude Code)
    python server.py --transport http   # HTTP transport (for remote clients)
"""

import sys
import json
import logging
import urllib.parse
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# ---------------------------------------------------------------------------
# Logging — stderr only (stdout is reserved for MCP JSON-RPC on stdio)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("datadawn-mcp")


# ---------------------------------------------------------------------------
# Tool-call logging decorator: per-call tool name + elapsed-ms instrumentation
# (logs arg names only, never values).
#
# FastMCP introspects the wrapped function via inspect.signature() to build
# the tool's JSON-schema; functools.wraps + an explicit __signature__ set
# preserves that introspection through this decorator chain.
# ---------------------------------------------------------------------------
import functools
import inspect
import time


def _log_tool_call(fn):
    """Wrap an async tool function to log tool_call / tool_done with elapsed ms."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        start = time.monotonic()
        # arg names only; never log values (could contain SQL, names, etc).
        arg_keys = sorted(kwargs.keys()) if kwargs else []
        logger.info("tool_call name=%s args=%s", fn.__name__, arg_keys)
        try:
            result = await fn(*args, **kwargs)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.info("tool_done name=%s ms=%d", fn.__name__, elapsed_ms)
            return result
        except Exception:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.exception("tool_error name=%s ms=%d", fn.__name__, elapsed_ms)
            raise

    wrapper.__signature__ = sig
    wrapper.__wrapped__ = fn
    return wrapper


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BASE_990 = "https://data.datadawn.org"
BASE_OPENREGS = "https://regs.datadawn.org"
DB_990 = "990data_public"
DB_OPENREGS = "openregs"

USER_AGENT = "DataDawn-MCP/1.0 (github.com/DataDawn-org)"
DEFAULT_LIMIT = 25
TIMEOUT = 30.0

# ---------------------------------------------------------------------------
# FastMCP server instance
# ---------------------------------------------------------------------------
# Derive transport-security host/origin allowlists from BASE_* so adding a new
# public base automatically extends both lists (audit 2026-05-10 H23).
_PUBLIC_HOSTS = [urllib.parse.urlparse(u).hostname for u in (BASE_990, BASE_OPENREGS)]

mcp = FastMCP(
    "DataDawn",
    instructions=(
        "DataDawn provides access to two large public-interest databases:\n"
        "1) The 990 Database (data.datadawn.org) — IRS 990 nonprofit filings, "
        "foundation grants, DAF disbursements, officers, and the IRS Business "
        "Master File for ~2M tax-exempt organizations.\n"
        "2) The OpenRegs Database (regs.datadawn.org) — Federal Register "
        "documents, regulations.gov dockets/documents/comments, congressional "
        "legislation, floor speeches, stock trades, lobbying filings, campaign "
        "finance, hearings, nominations, and more.\n\n"
        "Use the search/lookup tools for common queries. Use run_990_sql or "
        "run_openregs_sql for anything the structured tools don't cover."
    ),
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=(
            ["127.0.0.1:*", "localhost:*"]
            + _PUBLIC_HOSTS
            + [f"{h}:*" for h in _PUBLIC_HOSTS]
        ),
        allowed_origins=(
            [BASE_990, BASE_OPENREGS]
            + ["http://127.0.0.1:*", "http://localhost:*"]
        ),
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _query(base_url: str, db: str, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
    """Execute a SQL query against a Datasette instance and return rows."""
    url = f"{base_url}/{db}.json"
    query_params: dict[str, str] = {"sql": sql, "_shape": "objects"}
    if params:
        for k, v in params.items():
            query_params[k] = str(v)

    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, params=query_params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data.get("rows", [])


def _fmt_rows(rows: list[dict], max_rows: int = 50) -> str:
    """Format rows into a readable text table."""
    if not rows:
        return "No results found."

    total = len(rows)                      # capture BEFORE truncation so we can signal "of N"
    rows = rows[:max_rows]
    # Columns present in the result set but NULL/empty in EVERY returned row.
    # Surfaced explicitly so an agent's coverage query ("is column X populated?")
    # isn't silently misled into reading an empty column as an absent one (the
    # per-row null-skip below keeps wide tables readable but hides this on its own).
    all_keys = {k for r in rows for k in r}
    empty_cols = sorted(k for k in all_keys if all(r.get(k) in (None, "") for r in rows))
    lines: list[str] = []
    for i, row in enumerate(rows, 1):
        parts = []
        for k, v in row.items():
            if v is None or v == "":
                continue
            parts.append(f"  {k}: {v}")
        lines.append(f"--- Result {i} ---")
        lines.extend(parts)
    notes: list[str] = []
    if total > max_rows:
        notes.append(f"showing first {len(rows)} of {total} rows — add LIMIT or refine to see more")
    else:
        notes.append(f"{len(rows)} result{'s' if len(rows) != 1 else ''} shown")
    if empty_cols:
        notes.append(f"columns present but empty in all rows: {', '.join(empty_cols)}")
    return "\n".join(lines) + "\n(" + "; ".join(notes) + ")"


def _fmt_money(val: Any) -> str:
    """Format a dollar amount."""
    if val is None:
        return "N/A"
    try:
        return f"${int(val):,}"
    except (ValueError, TypeError):
        return str(val)


# ===================================================================
# 990 DATABASE TOOLS
# ===================================================================

@mcp.tool()
@_log_tool_call
async def search_nonprofit(query: str, state: str | None = None) -> str:
    """Search for tax-exempt nonprofits by name in the IRS Business Master File (~2M orgs).

    Returns: name, EIN, city, state, NTEE code, total assets, gross income.
    Use this as the starting point to find an organization's EIN, then use
    lookup_ein() or org_officers() for details.

    Args:
        query: Organization name or partial name to search for.
        state: Optional two-letter state code to filter results (e.g. "CA", "NY").
    """
    where = ""
    params = {"q": query}
    if state:
        where = "AND state = :state"
        params["state"] = state.upper()[:2]

    count_sql = f"""
        SELECT COUNT(*) AS n
        FROM bmf
        WHERE rowid IN (SELECT rowid FROM fts_bmf WHERE fts_bmf MATCH :q)
        {where}
    """
    sql = f"""
        SELECT name, ein, city, state, ntee_cd, subsection,
               asset_amt, income_amt
        FROM bmf
        WHERE rowid IN (
            SELECT rowid FROM fts_bmf WHERE fts_bmf MATCH :q
        )
        {where}
        ORDER BY income_amt DESC
        LIMIT {DEFAULT_LIMIT}
    """
    count_rows = await _query(BASE_990, DB_990, count_sql, params)
    total = count_rows[0].get("n", 0) if count_rows else 0
    rows = await _query(BASE_990, DB_990, sql, params)

    lines = []
    for r in rows:
        sub = r.get("subsection", "")
        sub_label = {"03": "501(c)(3)", "04": "501(c)(4)", "05": "501(c)(5)",
                     "06": "501(c)(6)"}.get(sub, f"501(c)({sub})" if sub else "")
        lines.append(
            f"  {r['name']}  |  EIN: {r['ein']}  |  {r.get('city','')}, {r.get('state','')}"
            f"  |  NTEE: {r.get('ntee_cd','N/A')}  |  {sub_label}"
            f"  |  Assets: {_fmt_money(r.get('asset_amt'))}  |  Income: {_fmt_money(r.get('income_amt'))}"
        )
    if not lines:
        return f"No nonprofits found matching '{query}'."
    if total > len(lines):
        header = f"Found {total:,} nonprofit(s) (showing top {len(lines)} by income):"
    else:
        header = f"Found {total:,} nonprofit(s):"
    return header + "\n\n" + "\n".join(lines)


@mcp.tool()
@_log_tool_call
async def lookup_ein(ein: str) -> str:
    """Look up a specific organization by EIN. Returns BMF reference data
    plus the 5 most recent IRS 990 filings with revenue, expenses, and assets.

    Args:
        ein: 9-digit Employer Identification Number (with or without dash).
    """
    ein = ein.replace("-", "").strip()

    # BMF info
    bmf_sql = """
        SELECT name, ein, city, state, ntee_cd, subsection,
               foundation, asset_amt, income_amt
        FROM bmf WHERE ein = :ein
    """
    bmf = await _query(BASE_990, DB_990, bmf_sql, {"ein": ein})

    # Recent filings
    filings_sql = """
        SELECT tax_year, return_type, total_revenue, total_expenses,
               total_assets_eoy, contributions_received
        FROM returns
        WHERE ein = :ein AND return_type IN ('990', '990EZ', '990PF')
        ORDER BY tax_year DESC
        LIMIT 5
    """
    filings = await _query(BASE_990, DB_990, filings_sql, {"ein": ein})

    parts = []
    if bmf:
        b = bmf[0]
        sub = b.get("subsection", "")
        sub_label = {"03": "501(c)(3)", "04": "501(c)(4)"}.get(sub, f"501(c)({sub})" if sub else "Unknown")
        parts.append(
            f"Organization: {b['name']}\n"
            f"EIN: {b['ein']}\n"
            f"Location: {b.get('city','')}, {b.get('state','')}\n"
            f"Type: {sub_label}\n"
            f"NTEE Code: {b.get('ntee_cd','N/A')}\n"
            f"Assets (BMF): {_fmt_money(b.get('asset_amt'))}\n"
            f"Income (BMF): {_fmt_money(b.get('income_amt'))}"
        )
    else:
        parts.append(f"No BMF record found for EIN {ein}.")

    if filings:
        parts.append("\nRecent Filings:")
        for f in filings:
            parts.append(
                f"  {f['tax_year']} ({f['return_type']}): "
                f"Revenue {_fmt_money(f.get('total_revenue'))} | "
                f"Expenses {_fmt_money(f.get('total_expenses'))} | "
                f"Assets {_fmt_money(f.get('total_assets_eoy'))}"
            )
    else:
        parts.append("\nNo 990 filings found for this EIN.")

    return "\n".join(parts)


@mcp.tool()
@_log_tool_call
async def search_grants(recipient: str, min_amount: int | None = None) -> str:
    """Search foundation grants (from 990PF filings) by recipient name.
    The grants table has ~12.5M rows. Returns funder name/EIN, recipient,
    amount, purpose, and tax year.

    Args:
        recipient: Recipient name or partial name to search for.
        min_amount: Optional minimum grant amount in dollars to filter results.
    """
    having = ""
    if min_amount:
        having = f"AND g.amount >= {int(min_amount)}"

    count_sql = f"""
        SELECT COUNT(*) AS n
        FROM grants g
        WHERE g.id IN (SELECT rowid FROM fts_grants WHERE fts_grants MATCH :q)
        AND g.grant_type = 'paid'
        {having}
    """
    sql = f"""
        SELECT g.recipient_name, g.amount, g.purpose, g.tax_year,
               g.ein AS funder_ein, r.org_name AS funder_name
        FROM grants g
        JOIN returns r ON g.object_id = r.object_id
        WHERE g.id IN (
            SELECT rowid FROM fts_grants WHERE fts_grants MATCH :q
        )
        AND g.grant_type = 'paid'
        {having}
        ORDER BY g.amount DESC
        LIMIT {DEFAULT_LIMIT}
    """
    count_rows = await _query(BASE_990, DB_990, count_sql, {"q": recipient})
    total = count_rows[0].get("n", 0) if count_rows else 0
    rows = await _query(BASE_990, DB_990, sql, {"q": recipient})

    lines = []
    for r in rows:
        lines.append(
            f"  {_fmt_money(r.get('amount'))} from {r.get('funder_name','?')} (EIN: {r.get('funder_ein','')})\n"
            f"    To: {r.get('recipient_name','')}\n"
            f"    Purpose: {r.get('purpose','N/A')}\n"
            f"    Year: {r.get('tax_year','')}"
        )
    if not lines:
        return f"No grants found for recipient matching '{recipient}'."
    if total > len(lines):
        header = f"Found {total:,} grant(s) (showing top {len(lines)} by amount):"
    else:
        header = f"Found {total:,} grant(s):"
    return header + "\n\n" + "\n\n".join(lines)


@mcp.tool()
@_log_tool_call
async def search_daf_grants(recipient: str) -> str:
    """Search Donor-Advised Fund (DAF) disbursements from Schedule I by
    recipient name. DAFs are intermediary giving vehicles — the funder_name
    is the sponsoring organization (e.g., Fidelity Charitable).

    Args:
        recipient: Recipient name or partial name to search for.
    """
    # Count first so the displayed header shows the true total even when the
    # result set is capped at DEFAULT_LIMIT — without this, a query that has
    # 1,255 hits but displays 25 looks indistinguishable from "only 25 exist",
    # which created a confusing signal during the 2026-05-10 DAF incident
    # triage. (Same pattern worth applying to other search_* tools later.)
    count_sql = """
        SELECT COUNT(*) AS n FROM schedule_i_grants
        WHERE rowid IN (SELECT rowid FROM fts_daf WHERE fts_daf MATCH :q)
    """
    sql = f"""
        SELECT recipient_name, recipient_ein, amount, tax_year,
               funder_name, funder_ein
        FROM schedule_i_grants
        WHERE rowid IN (
            SELECT rowid FROM fts_daf WHERE fts_daf MATCH :q
        )
        ORDER BY amount DESC
        LIMIT {DEFAULT_LIMIT}
    """
    count_rows = await _query(BASE_990, DB_990, count_sql, {"q": recipient})
    total = count_rows[0].get("n", 0) if count_rows else 0
    rows = await _query(BASE_990, DB_990, sql, {"q": recipient})

    lines = []
    for r in rows:
        lines.append(
            f"  {_fmt_money(r.get('amount'))} via {r.get('funder_name','?')} (EIN: {r.get('funder_ein','')})\n"
            f"    To: {r.get('recipient_name','')}"
            f"  (EIN: {r.get('recipient_ein','N/A')})\n"
            f"    Year: {r.get('tax_year','')}"
        )
    if not lines:
        return f"No DAF grants found for recipient matching '{recipient}'."
    if total > len(lines):
        header = f"Found {total:,} DAF disbursement(s) (showing top {len(lines)} by amount):"
    else:
        header = f"Found {total:,} DAF disbursement(s):"
    return header + "\n\n" + "\n\n".join(lines)


@mcp.tool()
@_log_tool_call
async def org_officers(ein: str) -> str:
    """Get officers, directors, trustees, and key employees for an organization.
    Returns data from the most recent 990 filing.

    Args:
        ein: 9-digit Employer Identification Number.
    """
    ein = ein.replace("-", "").strip()
    # 5-column comp schema per Bug #3 fix (decisions_log §64).
    # reportable_comp_filing_org = W-2 from filing org (all forms);
    # other_comp = sum of remaining IRS comp boxes per form (Form 990:
    # related-org W-2 + other comp; 990-EZ/PF: benefits + expense_account).
    sql = f"""
        SELECT o.person_name, o.title,
               o.reportable_comp_filing_org,
               (COALESCE(o.reportable_comp_related_org, 0)
                + COALESCE(o.other_compensation, 0)
                + COALESCE(o.benefits, 0)
                + COALESCE(o.expense_account, 0)) AS other_comp,
               r.tax_year, r.org_name
        FROM officers o
        JOIN returns r ON o.object_id = r.object_id
        -- Scope to the SINGLE canonical (latest) filing's object_id, NOT
        -- `tax_year = MAX(tax_year)`: an org can have >1 filing at its max year
        -- (amended/re-filed), and the bare tax_year match returns officers from
        -- ALL of them -> double-counted rows. object_id is unique (PK) so the
        -- (tax_year DESC, object_id DESC) pick is deterministic (latest submission
        -- wins). Mirrors the org/{ein}.html officers display path. followup_queue #291.
        WHERE o.object_id = (
            SELECT r2.object_id FROM returns r2
            WHERE r2.ein = :ein AND r2.return_type IN ('990','990EZ','990PF')
            ORDER BY r2.tax_year DESC, r2.object_id DESC
            LIMIT 1
        )
        ORDER BY o.reportable_comp_filing_org DESC
        LIMIT 50
    """
    rows = await _query(BASE_990, DB_990, sql, {"ein": ein})

    if not rows:
        return f"No officers found for EIN {ein}."

    org = rows[0].get("org_name", "Unknown")
    year = rows[0].get("tax_year", "?")
    lines = [f"Officers/Directors for {org} (EIN: {ein}, Tax Year {year}):\n"]
    for r in rows:
        comp = _fmt_money(r.get("reportable_comp_filing_org"))
        other = _fmt_money(r.get("other_comp"))
        lines.append(
            f"  {r.get('person_name','?')}  |  {r.get('title','?')}"
            f"  |  Comp from Filing Org: {comp}  |  Other Comp: {other}"
        )
    return "\n".join(lines)


@mcp.tool()
@_log_tool_call
async def org_grants_made(ein: str, limit: int = 50) -> str:
    """Get grants made by a private foundation (by funder EIN).
    Only returns grants with grant_type='paid'. The ein in the grants
    table is the FUNDER, not the recipient.

    Args:
        ein: 9-digit EIN of the grant-making foundation.
        limit: Max number of grants to return (default 50).
    """
    ein = ein.replace("-", "").strip()
    limit = min(int(limit), 100)

    sql = f"""
        SELECT g.recipient_name, g.recipient_state, g.amount,
               g.purpose, g.tax_year
        FROM grants g
        WHERE g.ein = :ein AND g.grant_type = 'paid'
        ORDER BY g.tax_year DESC, g.amount DESC
        LIMIT {limit}
    """
    rows = await _query(BASE_990, DB_990, sql, {"ein": ein})

    if not rows:
        return f"No grants found for funder EIN {ein}."

    lines = [f"Grants made by EIN {ein} ({len(rows)} shown):\n"]
    for r in rows:
        lines.append(
            f"  {_fmt_money(r.get('amount'))} to {r.get('recipient_name','?')}"
            f" ({r.get('recipient_state','?')}) — {r.get('tax_year','')}\n"
            f"    Purpose: {r.get('purpose','N/A')}"
        )
    return "\n".join(lines)


@mcp.tool()
@_log_tool_call
async def run_990_sql(sql: str) -> str:
    """Run arbitrary read-only SQL against the 990 nonprofit database.
    The database has ~5M filings, ~12.5M grants, ~42M officer records,
    and ~2M BMF records. Key tables: returns, grants, officers, bmf,
    schedule_i_grants, capital_gains, investments, related_orgs.

    IMPORTANT: Always filter returns by return_type IN ('990','990EZ')
    to exclude 990-T filings. Filter grants by grant_type='paid' unless
    you want future commitments. The grants.ein is the FUNDER, not the
    recipient.

    COVERAGE: contractors are parsed from Form 990 and 990-PF filings —
    an empty result means none reported above the $100K threshold.
    top_employees covers Form 990-PF; Form-990 highest-compensated
    employees appear in the officers table flagged
    is_highest_compensated_employee (not duplicated in top_employees).

    Args:
        sql: A SELECT query to run. Must be read-only. Include LIMIT clause.
    """
    sql = sql.strip()
    if not sql.upper().startswith("SELECT"):
        return "Error: Only SELECT queries are allowed."

    try:
        rows = await _query(BASE_990, DB_990, sql)
        return _fmt_rows(rows)
    except httpx.HTTPStatusError as e:
        return f"Query error (HTTP {e.response.status_code}): {e.response.text[:500]}"
    except Exception as e:
        return f"Query error: {str(e)}"


# ===================================================================
# OPENREGS DATABASE TOOLS
# ===================================================================

@mcp.tool()
@_log_tool_call
async def search_federal_register(query: str, type: str | None = None, year: int | None = None) -> str:
    """Search Federal Register documents (~1M documents, 1994-present) by keyword.
    Covers rules, proposed rules, notices, and presidential documents.

    Args:
        query: Search keywords (uses full-text search).
        type: Optional filter: "Rule", "Proposed Rule", "Notice", or "Presidential Document".
        year: Optional publication year to filter by.
    """
    where_parts = []
    params = {"q": query}
    if type:
        where_parts.append("AND fr.type = :type")
        params["type"] = type
    if year:
        where_parts.append(f"AND fr.pub_year = {int(year)}")
    where = " ".join(where_parts)

    count_sql = f"""
        SELECT COUNT(*) AS n
        FROM federal_register fr
        WHERE fr.rowid IN (SELECT rowid FROM federal_register_fts WHERE federal_register_fts MATCH :q)
        {where}
    """
    sql = f"""
        SELECT fr.document_number, fr.title, fr.type, fr.publication_date,
               fr.agency_names, fr.abstract
        FROM federal_register fr
        WHERE fr.rowid IN (
            SELECT rowid FROM federal_register_fts
            WHERE federal_register_fts MATCH :q
        )
        {where}
        ORDER BY fr.publication_date DESC
        LIMIT {DEFAULT_LIMIT}
    """
    count_rows = await _query(BASE_OPENREGS, DB_OPENREGS, count_sql, params)
    total = count_rows[0].get("n", 0) if count_rows else 0
    rows = await _query(BASE_OPENREGS, DB_OPENREGS, sql, params)

    lines = []
    for r in rows:
        abstract = (r.get("abstract") or "")[:200]
        lines.append(
            f"  [{r.get('type','')}] {r.get('title','?')}\n"
            f"    Doc#: {r.get('document_number','')}  |  Date: {r.get('publication_date','')}\n"
            f"    Agency: {r.get('agency_names','')}\n"
            f"    {abstract}{'...' if len(r.get('abstract','') or '') > 200 else ''}"
        )
    if not lines:
        return f"No Federal Register documents found matching '{query}'."
    if total > len(lines):
        header = f"Found {total:,} document(s) (showing top {len(lines)} by date):"
    else:
        header = f"Found {total:,} document(s):"
    return header + "\n\n" + "\n\n".join(lines)


@mcp.tool()
@_log_tool_call
async def search_legislation(query: str, congress: int | None = None) -> str:
    """Search congressional bills and resolutions (~168K records, Congresses 93-119).
    Returns bill ID, title, policy area, sponsor, cosponsor count, and summary.

    Args:
        query: Search keywords (uses full-text search on titles and summaries).
        congress: Optional Congress number to filter by (e.g. 118 for the 118th Congress).
    """
    where = ""
    if congress:
        where = f"AND l.congress = {int(congress)}"

    count_sql = f"""
        SELECT COUNT(*) AS n
        FROM legislation l
        WHERE l.bill_id IN (SELECT bill_id FROM legislation_fts WHERE legislation_fts MATCH :q)
        {where}
    """
    sql = f"""
        SELECT l.bill_id, l.title, l.congress, l.bill_type, l.bill_number,
               l.policy_area, l.cosponsor_count,
               cm.full_name AS sponsor_name, cm.party, cm.state
        FROM legislation l
        LEFT JOIN congress_members cm ON l.sponsor_bioguide_id = cm.bioguide_id
        WHERE l.bill_id IN (
            SELECT bill_id FROM legislation_fts
            WHERE legislation_fts MATCH :q
        )
        {where}
        ORDER BY l.congress DESC, l.cosponsor_count DESC
        LIMIT {DEFAULT_LIMIT}
    """
    count_rows = await _query(BASE_OPENREGS, DB_OPENREGS, count_sql, {"q": query})
    total = count_rows[0].get("n", 0) if count_rows else 0
    rows = await _query(BASE_OPENREGS, DB_OPENREGS, sql, {"q": query})

    lines = []
    for r in rows:
        sponsor = r.get("sponsor_name", "?")
        party = r.get("party", "")
        state = r.get("state", "")
        sponsor_str = f"{sponsor} ({party[0] if party else '?'}-{state})" if sponsor != "?" else "?"
        lines.append(
            f"  {r.get('bill_id','?')}: {r.get('title','?')}\n"
            f"    Congress: {r.get('congress','')} | Policy Area: {r.get('policy_area','N/A')}\n"
            f"    Sponsor: {sponsor_str} | Cosponsors: {r.get('cosponsor_count',0)}"
        )
    if not lines:
        return f"No legislation found matching '{query}'."
    if total > len(lines):
        header = f"Found {total:,} bill(s) (showing top {len(lines)} by Congress + cosponsors):"
    else:
        header = f"Found {total:,} bill(s):"
    return header + "\n\n" + "\n\n".join(lines)


@mcp.tool()
@_log_tool_call
async def lookup_member(name: str) -> str:
    """Search for a member of Congress by name. Returns bioguide ID, party,
    state, chamber, and whether they currently serve. The bioguide_id is
    the universal key that links to stock trades, votes, speeches, etc.

    Args:
        name: Full or partial name of the member (e.g. "Pelosi", "Ted Cruz").
    """
    sql = f"""
        SELECT bioguide_id, full_name, party, state, chamber, is_current
        FROM congress_members
        WHERE full_name LIKE :q
        ORDER BY is_current DESC, full_name
        LIMIT {DEFAULT_LIMIT}
    """
    rows = await _query(BASE_OPENREGS, DB_OPENREGS, sql, {"q": f"%{name}%"})

    lines = []
    for r in rows:
        current = "Current" if r.get("is_current") else "Former"
        lines.append(
            f"  {r.get('full_name','')} ({r.get('party','?')}-{r.get('state','?')})"
            f"  |  {r.get('chamber','')}  |  {current}"
            f"  |  Bioguide: {r.get('bioguide_id','')}"
        )
    if not lines:
        return f"No members found matching '{name}'."
    return f"Found {len(lines)} member(s):\n\n" + "\n".join(lines)


@mcp.tool()
@_log_tool_call
async def member_trades(bioguide_id: str) -> str:
    """Get stock trades disclosed by a member of Congress. Returns ticker,
    asset description, transaction type, amount range, and date.
    Use lookup_member() first to find the bioguide_id.

    Args:
        bioguide_id: The member's bioguide ID (e.g. "P000197" for Pelosi).
    """
    sql = f"""
        SELECT st.transaction_date, st.ticker, st.asset_description,
               st.transaction_type, st.amount_range, st.chamber,
               cm.full_name
        FROM stock_trades st
        LEFT JOIN congress_members cm ON st.bioguide_id = cm.bioguide_id
        WHERE st.bioguide_id = :bid
        ORDER BY st.transaction_date DESC
        LIMIT 50
    """
    rows = await _query(BASE_OPENREGS, DB_OPENREGS, sql, {"bid": bioguide_id})

    if not rows:
        return f"No stock trades found for bioguide_id '{bioguide_id}'."

    member = rows[0].get("full_name", bioguide_id)
    lines = [f"Stock trades for {member} ({len(rows)} shown):\n"]
    for r in rows:
        lines.append(
            f"  {r.get('transaction_date','')}  |  {r.get('ticker','N/A')}"
            f"  |  {r.get('transaction_type','?')}  |  {r.get('amount_range','?')}\n"
            f"    {r.get('asset_description','')}"
        )
    return "\n".join(lines)


@mcp.tool()
@_log_tool_call
async def search_lobbying(query: str, year: int | None = None) -> str:
    """Search lobbying disclosure filings (~1.9M filings, 1999-2026) by
    client or registrant name. Returns registrant, client, issue areas,
    and filing-level income/expense amounts.

    Scope: LD-2 quarterly activity reports only (the "lobbying spending" public
    concept). LD-203 contribution reports are excluded from results. To see all
    filing types in custom SQL, query lobbying_filings without the
    filing_type GLOB '[1234Q]*' filter.

    Args:
        query: Client or registrant name to search for.
        year: Optional year to filter by.
    """
    where = ""
    if year:
        where = f"AND la.filing_year = {int(year)}"

    # FTS match on the activity table; JOIN to lobbying_filings for canonical
    # filing-level amount + LD-2 scope filter; DISTINCT collapses multiple
    # activity rows from the same filing into one result row.
    count_sql = f"""
        SELECT COUNT(DISTINCT la.filing_uuid) AS n
        FROM lobbying_activities la
        JOIN lobbying_filings f ON f.filing_uuid = la.filing_uuid
        WHERE la.rowid IN (SELECT rowid FROM lobbying_fts WHERE lobbying_fts MATCH :q)
          AND f.filing_type GLOB '[1234Q]*'
        {where}
    """
    sql = f"""
        SELECT DISTINCT la.registrant_name, la.client_name, la.filing_year,
               la.issue_code, la.specific_issues,
               f.income_amount, f.expense_amount
        FROM lobbying_activities la
        JOIN lobbying_filings f ON f.filing_uuid = la.filing_uuid
        WHERE la.rowid IN (
            SELECT rowid FROM lobbying_fts WHERE lobbying_fts MATCH :q
        )
          AND f.filing_type GLOB '[1234Q]*'
        {where}
        ORDER BY COALESCE(f.income_amount, f.expense_amount) DESC
        LIMIT {DEFAULT_LIMIT}
    """
    count_rows = await _query(BASE_OPENREGS, DB_OPENREGS, count_sql, {"q": query})
    total = count_rows[0].get("n", 0) if count_rows else 0
    rows = await _query(BASE_OPENREGS, DB_OPENREGS, sql, {"q": query})

    lines = []
    for r in rows:
        issues = (r.get("specific_issues") or "")[:150]
        # Show whichever side of the income/expense XOR is populated.
        # If both are NULL (shouldn't happen on LD-2 but be defensive), show "—".
        inc = r.get("income_amount")
        exp = r.get("expense_amount")
        if inc:
            money = f"Income: {_fmt_money(inc)}"
        elif exp:
            money = f"Expenses: {_fmt_money(exp)} (in-house)"
        else:
            money = "—"
        lines.append(
            f"  {r.get('registrant_name','?')} for {r.get('client_name','?')}"
            f"  ({r.get('filing_year','')})  |  {money}\n"
            f"    Issue: {r.get('issue_code','')} — {issues}"
            f"{'...' if len(r.get('specific_issues','') or '') > 150 else ''}"
        )
    if not lines:
        return f"No lobbying filings found matching '{query}'."
    if total > len(lines):
        header = f"Found {total:,} filing(s) (showing top {len(lines)} by amount):"
    else:
        header = f"Found {total:,} filing(s):"
    return header + "\n\n" + "\n\n".join(lines)


@mcp.tool()
@_log_tool_call
async def search_comments(query: str, agency: str | None = None) -> str:
    """Search public comments on federal regulations (~9.9M comment headers).
    Most comments are from FWS, EPA, FDA, and APHIS. Returns comment ID,
    title, submitter, agency, docket, and date.

    Args:
        query: Search keywords (uses full-text search on comment titles).
        agency: Optional agency code to filter (e.g. "EPA", "FDA", "FWS").
    """
    where = ""
    params = {"q": query}
    if agency:
        where = "AND c.agency_id = :agency"
        params["agency"] = agency.upper()[:10]

    count_sql = f"""
        SELECT COUNT(*) AS n
        FROM comments c
        WHERE c.rowid IN (SELECT rowid FROM comments_fts WHERE comments_fts MATCH :q)
        {where}
    """
    sql = f"""
        SELECT c.id, c.title, c.submitter_name, c.submitter_type,
               c.agency_id, c.docket_id, c.posted_date
        FROM comments c
        WHERE c.rowid IN (
            SELECT rowid FROM comments_fts WHERE comments_fts MATCH :q
        )
        {where}
        ORDER BY c.posted_date DESC
        LIMIT {DEFAULT_LIMIT}
    """
    count_rows = await _query(BASE_OPENREGS, DB_OPENREGS, count_sql, params)
    total = count_rows[0].get("n", 0) if count_rows else 0
    rows = await _query(BASE_OPENREGS, DB_OPENREGS, sql, params)

    lines = []
    for r in rows:
        lines.append(
            f"  {r.get('id','?')}: {r.get('title','(no title)')}\n"
            f"    By: {r.get('submitter_name','Anonymous')} ({r.get('submitter_type','?')})"
            f"  |  Agency: {r.get('agency_id','')}  |  Docket: {r.get('docket_id','')}"
            f"  |  Date: {r.get('posted_date','')}"
        )
    if not lines:
        return f"No comments found matching '{query}'."
    if total > len(lines):
        header = f"Found {total:,} comment(s) (showing top {len(lines)} by date):"
    else:
        header = f"Found {total:,} comment(s):"
    return header + "\n\n" + "\n\n".join(lines)


@mcp.tool()
@_log_tool_call
async def run_openregs_sql(sql: str) -> str:
    """Run arbitrary read-only SQL against the OpenRegs government database.

    Key tables and row counts:
    - federal_register (~994K) — FR documents 1994-present
    - dockets (~165K), documents (~1.2M), comments (~9.9M) — Regulations.gov
    - legislation (~168K), legislation_actions, legislation_cosponsors
    - congress_members (~12.8K), committee_memberships, committees
    - congressional_record (~879K), crec_speakers, crec_bills
    - stock_trades (~61K) — Congressional stock disclosures
    - lobbying_activities (~3.5M), lobbying_lobbyists (~4.4M)
    - roll_call_votes (~26K), member_votes (~8.3M)
    - spending_awards (~864K), cfr_sections (~123K)
    - hearings (~46K), hearing_witnesses, hearing_members
    - fec_contributions (~4.4M), fec_candidates, fec_committees
    - fara_registrants, fara_foreign_principals, fara_short_forms
    - nominations (~40K), treaties (~777)
    - crs_reports (~13.6K), gao_reports (~16.6K)
    - presidential_documents (~5.9K)

    There are also many pre-computed summary tables (docket_stats,
    speaker_activity, lobbying_by_year, etc.) and cross-reference
    tables (witness_lobby_overlap, speeches_near_trades, etc.).

    Args:
        sql: A SELECT query to run. Must be read-only. Include LIMIT clause.
    """
    sql = sql.strip()
    if not sql.upper().startswith("SELECT"):
        return "Error: Only SELECT queries are allowed."

    try:
        rows = await _query(BASE_OPENREGS, DB_OPENREGS, sql)
        return _fmt_rows(rows)
    except httpx.HTTPStatusError as e:
        return f"Query error (HTTP {e.response.status_code}): {e.response.text[:500]}"
    except Exception as e:
        return f"Query error: {str(e)}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="DataDawn MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport type (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP transport")
    parser.add_argument("--port", type=int, default=8006, help="Port for HTTP transport")
    args = parser.parse_args()

    if args.transport == "streamable-http":
        logger.info(f"Starting DataDawn MCP server on {args.host}:{args.port} (HTTP)")
        # Set host/port on the server settings before running
        mcp.settings.host = args.host
        mcp.settings.port = args.port
    else:
        logger.info("Starting DataDawn MCP server (stdio)")

    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
