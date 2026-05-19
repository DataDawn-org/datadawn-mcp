# DataDawn MCP Server

An MCP (Model Context Protocol) server that gives AI agents direct access to two large public-interest databases:

- **990 Database** (data.datadawn.org) — IRS 990 nonprofit filings, foundation grants, DAF disbursements, officers, and the IRS Business Master File for ~2M tax-exempt organizations.
- **OpenRegs Database** (regs.datadawn.org) — Federal Register documents, regulations.gov dockets/documents/comments, congressional legislation, floor speeches, stock trades, lobbying filings, campaign finance, hearings, nominations, and more.

The server proxies queries to the Datasette JSON API — no API keys or database files needed.

## Install

```bash
pip install mcp httpx
```

Or with uv:

```bash
uv pip install mcp httpx
```

## Tools

### 990 Database (data.datadawn.org)

| Tool | Description |
|------|-------------|
| `search_nonprofit(query, state?)` | Search the IRS Business Master File by org name. Starting point for finding EINs. |
| `lookup_ein(ein)` | Look up an org by EIN. Returns BMF data + recent 990 filings with financials. |
| `search_grants(recipient, min_amount?)` | Search foundation grants (~12.5M) by recipient name. |
| `search_daf_grants(recipient)` | Search Donor-Advised Fund disbursements by recipient name. |
| `org_officers(ein)` | Get officers/directors/key employees for an org (most recent filing). |
| `org_grants_made(ein, limit?)` | Get grants made by a foundation (by funder EIN). |
| `run_990_sql(sql)` | Run arbitrary read-only SQL against the 990 database. |

### OpenRegs Database (regs.datadawn.org)

| Tool | Description |
|------|-------------|
| `search_federal_register(query, type?, year?)` | Search ~1M Federal Register documents (1994-present). |
| `search_legislation(query, congress?)` | Search congressional bills (~168K, Congresses 93-119). |
| `lookup_member(name)` | Find a member of Congress by name. Returns bioguide_id for cross-referencing. |
| `member_trades(bioguide_id)` | Get stock trades disclosed by a member of Congress. |
| `search_lobbying(query, year?)` | Search ~1.9M lobbying filings by client or registrant. |
| `search_comments(query, agency?)` | Search ~3.9M public comments on federal regulations. |
| `run_openregs_sql(sql)` | Run arbitrary read-only SQL against the OpenRegs database. |

## Add to Claude Code

### Option 1: CLI command (recommended)

```bash
claude mcp add --transport stdio --scope user datadawn -- python /path/to/datadawn-mcp/server.py
```

This makes the server available in all your Claude Code sessions.

### Option 2: Project-scoped

```bash
claude mcp add --transport stdio datadawn -- python /path/to/datadawn-mcp/server.py
```

### Option 3: Manual JSON config

Add to `~/.claude.json` under the `mcpServers` key:

```json
{
  "mcpServers": {
    "datadawn": {
      "type": "stdio",
      "command": "python",
      "args": ["/path/to/datadawn-mcp/server.py"]
    }
  }
}
```

### Verify

After adding, restart Claude Code (or start a new session) and run:

```
/mcp
```

You should see "datadawn" listed with 14 tools available.

## Test

### Quick test with MCP CLI

```bash
cd /path/to/datadawn-mcp
mcp dev server.py
```

This opens the MCP Inspector in your browser where you can call tools interactively.

### Test from Python

```python
import asyncio
import httpx

async def test():
    # Direct Datasette query (same thing the MCP server does internally)
    url = "https://data.datadawn.org/990data_public.json"
    params = {
        "sql": "SELECT name, ein, state FROM bmf WHERE ein IN (SELECT ein FROM fts_bmf WHERE fts_bmf MATCH 'ford foundation') LIMIT 5",
        "_shape": "objects",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params)
        print(resp.json()["rows"])

asyncio.run(test())
```

### Test with Claude Code

After adding the MCP server, try these prompts in Claude Code:

```
Search for the Ford Foundation's EIN and recent filings.
```

```
What stock trades has Nancy Pelosi disclosed?
```

```
Find lobbying filings by Google in 2024.
```

```
Search for EPA rules about PFAS chemicals.
```

## How it works

Each tool constructs a SQL query, sends it to the Datasette JSON API via HTTP, and formats the response as human-readable text. The server uses the `stdio` transport by default, communicating with Claude Code over stdin/stdout using JSON-RPC.

```
Claude Code <--stdio--> server.py <--HTTPS--> Datasette JSON API
                                                 |
                                       data.datadawn.org (990)
                                       regs.datadawn.org (OpenRegs)
```

## Notes

- All queries are read-only SELECT statements. The Datasette API enforces this.
- Results are limited to 25-50 rows by default to keep responses manageable.
- The `run_990_sql` and `run_openregs_sql` tools accept arbitrary SQL for advanced queries.
- For the 990 database: always filter `return_type IN ('990','990EZ')` to exclude 990-T filings, and filter `grant_type = 'paid'` on grants unless you want future commitments.
- Full schema documentation lives in the public companion repos: [openregulations-public](https://github.com/DataDawn-org/openregulations-public) (`openregs_schema.md`) and [990database-public](https://github.com/DataDawn-org/990database-public) (`990_schema.md`).

## License

CC0 1.0 Universal — see [LICENSE](LICENSE).
