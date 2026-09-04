# Flipkart GRN Schedulers

Four Flipkart vendor pipelines that share one Google account
(`flipkartquick@thebakersdozen.in`), one spreadsheet and one OAuth token — so
they live in one repo. Every other scheduler repo in this project holds exactly
one pipeline; this is the deliberate exception.

Each pipeline pulls its documents out of Gmail or Drive, extracts the line items
with a LlamaCloud agent, and writes them to **both** a Google Sheets tab and a
Supabase table.

## The four pipelines

| App file | LlamaCloud agent | Sheet tab | Supabase table | `--source` |
|---|---|---|---|---|
| `app_cp_grn.py` | Flipkart Crop Basket GRN | `cbgrn` | `flipkart_cb_grn` | `flipkart_cb` |
| `app_fp_grn.py` | Flipkart PRR GRN | `pprgrn` | `flipkart_ppr_grn` | `flipkart_ppr` |
| `app_fat_grn.py` | Fatema GRN | `fatemagrn` | `fatema_grn` | `fatema` |
| `app_slv_grn.py` | SL Veggies GRN | `slveggiesgrn` | `slveggies_grn` | `slveggies` |

All four write to spreadsheet `1DNowuKF1gk0AVu2Ytt1wKZqCtzllhPIZTAwk5p7aCNQ`.

## Pipeline

```
Crop Basket / PRR:   Gmail ---> Drive ---> LlamaExtract ---> Sheets
                                    \---> LlamaExtract ---> Supabase

Fatema / SL Veggies:         Drive ---> LlamaExtract ---> Sheets
                                    \---> LlamaExtract ---> Supabase
```

Crop Basket and PRR collect their own PDFs from Gmail first. Fatema and
SL Veggies expect the PDFs to already be sitting in their Drive folder.

## Two dependency sets — do not merge them

`app_cp_grn.py` and `app_fp_grn.py` import `llama_cloud.LlamaCloud`, the
rewritten SDK, which exists only in **`llama-cloud>=1.0`**. `supabase_sink.py`
and the other two apps need **`llama-cloud-services`**, which pins
`llama-cloud<0.2` and imports an `ExtractAgent` name that 2.x removed. Installing
both in one environment is impossible — pip refuses, and forcing it breaks the
import either way.

So there are two requirement files, and the Crop Basket and PRR pipelines each
get **two jobs on two runners**:

| File | Used by |
|---|---|
| `requirements.txt` | `supabase_sink.py`, `app_fat_grn.py`, `app_slv_grn.py` |
| `requirements-newsdk.txt` | `app_cp_grn.py`, `app_fp_grn.py` (their Sheets jobs only) |

## Schedule and entry points

`.github/workflows/scheduler.yml`, cron `0 */3 * * *` (every 3 hours), plus
manual **Run workflow**. Six jobs:

| Job | Installs | Runs |
|---|---|---|
| `crop-basket-sheets` | `requirements-newsdk.txt` | `run_combined_workflow()` |
| `crop-basket-supabase` | `requirements.txt` | `supabase_sink.py --app app_cp_grn.py --source flipkart_cb` |
| `prr-sheets` | `requirements-newsdk.txt` | `run_combined_workflow()` |
| `prr-supabase` | `requirements.txt` | `supabase_sink.py --app app_fp_grn.py --source flipkart_ppr` |
| `fatema` | `requirements.txt` | `run_automation()` then the sink |
| `slveggies` | `requirements.txt` | `run_automation()` then the sink |

The `-supabase` jobs `needs:` their `-sheets` job so the Gmail→Drive step has
already run. They use `if: always()`, so a Sheets failure still lets the files
already in Drive load.

The workflow deliberately does **not** call each script's `main()` /
`__main__` block — those spin a `schedule` loop for standalone local use, which
would sit idle burning runner minutes.

## Required secrets

Set under **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
|---|---|
| `GOOGLE_CREDENTIALS` | base64 of `credentials.json` (Google OAuth client) |
| `GOOGLE_TOKEN` | base64 of `token.json` (authorized refresh token) |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | service_role key — bypasses RLS so writes succeed |

There is deliberately **no `LLAMA_API_KEY` secret** here. This repo uses two
different LlamaCloud keys (Crop Basket + PRR share one, Fatema + SL Veggies
share another), so a single repo-level secret would be wrong for half the jobs.
Each app file carries its own key in `CONFIG`, and `supabase_sink.py` falls back
to it when `LLAMA_CLOUD_API_KEY` is unset.

> [!WARNING]
> Those two keys are committed in the app files' `CONFIG` dicts. They were left
> as-is on request. If you ever want them out, rotating them in LlamaCloud is the
> only thing that helps — deleting the literals does not, because they remain in
> git history.

The workflow base64-decodes the two Google secrets into `credentials.json` /
`token.json` at the start of each job and deletes them in an `if: always()`
cleanup step.

## Known limitation: Fatema and SL Veggies find 0 files

> [!IMPORTANT]
> The committed `token.json` was granted the **`drive.file`** scope, not full
> `drive`. `drive.file` only exposes files the app itself created.
>
> Crop Basket and PRR are fine — their Drive folders were populated by their own
> Gmail→Drive step, so they can see all 16 and 15 PDFs respectively. But the
> Fatema (`1F7uuAyh2PJlBzydUZRyRd0PtpgGp50R4`) and SL Veggies
> (`1QCBhTn2kohkc1fvZWtFGqJ5R3UeZ-3JX`) folders were created by hand, so the API
> returns **404** for them and both jobs report `found=0`.
>
> They fail safe — the jobs complete cleanly and write nothing, rather than
> erroring. **The fix needs no code change:** re-run the OAuth consent for
> `flipkartquick@thebakersdozen.in` requesting
> `https://www.googleapis.com/auth/drive`, then update the `GOOGLE_TOKEN` secret
> with the base64 of the new `token.json`.

## Running locally

```bash
py -3.12 -m venv .venv          # 3.12 or older; llama-cloud-services
.venv/Scripts/pip install -r requirements.txt   # breaks on 3.13+
# place credentials.json + token.json next to the scripts, and copy .env
```

`.env` sets a local default (`GRN_SOURCE` / `GRN_APP`) so you can omit the flags,
but because four apps sit side by side it is clearest to pass both:

```bash
python supabase_sink.py --list-sources
python supabase_sink.py --check
python supabase_sink.py --self-test
python supabase_sink.py --run --dry-run --limit 2 --app app_cp_grn.py --source flipkart_cb
python supabase_sink.py --run --app app_cp_grn.py --source flipkart_cb
```

Note `--days-back` when testing: Crop Basket and PRR default to
`days_back: 2`, so a quiet couple of days legitimately yields 0 files.

## Files

| File | Role |
|---|---|
| `app_cp_grn.py` | Crop Basket: Gmail → Drive → Sheets, class `MilkbasketAutomation` |
| `app_fp_grn.py` | PRR: Gmail → Drive → Sheets, class `MilkbasketAutomation` |
| `app_fat_grn.py` | Fatema: Drive → Sheets, class `SLVeggiesAutomation` |
| `app_slv_grn.py` | SL Veggies: Drive → Sheets, class `SLVeggiesAutomation` |
| `supabase_sink.py` | Drive → Supabase. **Identical in every scheduler repo** |
| `.github/workflows/scheduler.yml` | 3-hourly Actions schedule, 6 jobs |
| `requirements.txt` | deps for the sink, Fatema and SL Veggies |
| `requirements-newsdk.txt` | deps for Crop Basket and PRR Sheets jobs |

The class names are copy-paste leftovers — `app_cp_grn.py` and `app_fp_grn.py`
both call their class `MilkbasketAutomation`, and the Fatema script identifies
itself as "SL Veggies" in its docstring and log file. They are left untouched so
the Sheets behaviour is unchanged; `supabase_sink.py` finds the class by the
methods it exposes rather than by name, so the naming does not matter to it.

## Adding a field

Add it to the extract agent in LlamaCloud, then widen the header row on the
sheet tab. For Supabase, any new key already lands in `raw_data` and is queryable
as `raw_data->>'key'`. To promote it to a real column, add it to that source's
entry in `SOURCES` in `supabase_sink.py`, run `--print-schema --source <name>`,
and apply the `alter table` it emits.

## Disk guard (shared across every pipeline)

This scheduler writes to a Supabase volume shared with the Business Central
sync and the marketplace/ads loaders. Before it writes, it asks the database
whether it is allowed. **If you get an email titled `[WARN]` or `[STOP]
Supabase disk`, start here.**

```sql
-- the GRN schedulers genuinely need more room, and the volume has space:
UPDATE etl_disk_policy SET budget_gb = 8 WHERE pipeline = 'grn';

-- you resized the Supabase volume (do this EVERY time you resize):
UPDATE etl_disk_policy SET budget_gb = 100 WHERE pipeline = '_disk';

-- someone else should get the emails:
UPDATE etl_alert_config SET recipients = ARRAY['birbal@thebakersdozen.in'];
```

All thirteen GRN schedulers share one `grn` budget, because they are one
workload from the volume's point of view. A `[STOP]` means this scheduler is
refusing to write until you do one of the above. Nothing is lost: it stops
before writing and the next run continues.

`etl_alerts.py` is **identical in every pipeline repo** - do not add per-repo
logic to it. Everything configurable lives in Postgres (`etl_disk_policy`,
`etl_alert_config`), so budgets, thresholds and recipients change with an
`UPDATE` and no deploy, for all pipelines at once.

Three behaviours worth knowing:

- **No new credentials were needed.** This repo has no Postgres driver and no
  DSN, so the guard reaches the policy through PostgREST RPC using the
  `SUPABASE_URL` + service-role key it already holds.
- **It fails OPEN.** If the guard cannot run - credentials missing, database
  unreachable - it logs an error and lets the scheduler continue. A guard that
  breaks a working pipeline is worse than one that cannot check. Grep for
  `Disk guard could not run`.
- **Budgets grow themselves** into genuinely unallocated volume space, so a
  pipeline that is legitimately growing is not blocked by a number somebody
  guessed months ago. It can never grow past the volume ceiling.

Full documentation:
https://github.com/keyur-tbd/bc-supabase-sync#disk-alerts-and-auto-budgeting---start-here-if-you-got-an-email

## Birbal reads these tables (shared across every pipeline)

Since 2026-09-03 the Supabase project this writes to also backs **Birbal**
(`birbal-tbdai/birbal-mission-control`), the app the business asks questions in
plain language. Birbal never reads `public` directly: it reads one `select *`
view per table in a separate `warehouse` schema, plus a dictionary row per table
that tells it what the columns mean. Two consequences for this repo.

**A new table, or a new column, is invisible to Birbal until somebody exposes
it.** A view freezes its column list at CREATE time, and the exposure list is an
array inside a function - so nothing errors anywhere. The table simply does not
exist as far as the business is concerned, and an answer quietly leaves the new
column out. After applying the DDL this repo prints, run as `postgres`:

```sql
select app.sync_warehouse_views();   -- mirror new tables and columns
select app.sync_role_grants();       -- re-grant: the mirror drops grants
```

and add or update that table's row in `warehouse.warehouse_meta`. A column
nobody described there is a column Birbal will not use correctly.

**Never DROP or rename a table this pipeline owns.** A `warehouse` view depends
on it, so a plain `DROP` fails and `DROP ... CASCADE` deletes Birbal's view
without a word - that is how the BC sync went red on 2026-09-04. Add columns;
never replace tables. The writes themselves are safe by construction: every row
upserts on `row_hash`, so a reader never sees a half-loaded table.

Exposed today: all sixteen GRN/PRN tables the shared
`supabase_sink.py` knows about, including the one this repo loads (`GRN_SOURCE`).
Run `--print-schema` for the DDL when you promote a field.

Full contract, and the checks to run after a schema change:
https://github.com/keyur-tbd/bc-supabase-sync#who-reads-this-database-birbal
