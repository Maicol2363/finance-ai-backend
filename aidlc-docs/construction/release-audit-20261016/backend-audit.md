# Finance AI backend audit — 2026-10-16

## Scope and result

Reviewed the FastAPI routes, role model, authenticated financial operations, AI proposal confirmation, public error handling, OpenAPI contract, and automated backend suite. The review ran on `codex/release-audit-20261016`, based on `6c7c9fc`. No existing database was used; PostgreSQL persistence checks ran against a new disposable PostgreSQL 18.6 container with no mounted volumes.

Two release-relevant defects were corrected:

- Text AI confirmation now persists its proposal UUID on the resulting expense, income, or transfer. A unique `(user_id, source_proposal_id)` constraint prevents duplicate financial operations if the financial commit succeeds and the subsequent proposal update fails. A retry retrieves the prior operation and can finish storing the confirmation.
- Registration now resolves the `user` role by name instead of relying on role ID `1`. This prevents an existing database with a different role insertion order from assigning a new account the `admin` role.
- Unexpected API failures no longer call `logger.exception`; logs retain the correlation ID and exception type without attaching raw exception text or a traceback. The client continues to receive the stable `internal_error` response.
- A post-commit retry that changes confirmation values now builds the expense confirmation from the saved expense amount and timestamp. Income and transfer already returned saved values; new fault-injection cases verify all three intent paths return the original committed amount and date, with no duplicate movement or balance change.

## Route and role inventory

The checked-in OpenAPI contract describes 32 paths:

| Area | Paths | Access |
|---|---|---|
| Health | `GET /health/live`, `GET /health/ready` | Public |
| Authentication | `POST /api/v1/auth/register`, `/login`, `/refresh`, `/logout`; `GET /api/v1/auth/me` | Register/login/refresh do not require a bearer session; logout uses a renewable credential in its body; `/me` requires bearer access |
| Accounts | `/api/v1/accounts`, `/api/v1/accounts/{account_id}`, `/status` | Authenticated, owner-scoped |
| Budgets | `/api/v1/budgets`, `/api/v1/budgets/{budget_id}` | Authenticated, owner-scoped |
| Categories | `/api/v1/categories`, `/order`, `/{category_id}` | Authenticated, owner-scoped |
| Expenses and history | `/api/v1/expenses`, `/{expense_id}`, `/api/v1/history` | Authenticated, owner-scoped |
| Income and sources | `/api/v1/incomes`, `/{income_id}`, `/api/v1/income-sources`, `/{income_source_id}` | Authenticated, owner-scoped |
| Transfers | `POST /api/v1/transfers` | Authenticated, owner-scoped |
| Dashboard | `GET /api/v1/dashboard/summary` | Authenticated, owner-scoped |
| Receipt capture | `/api/v1/receipts/proposal`, `/{proposal_id}`, `/{proposal_id}/retry`, `/{proposal_id}/confirm`, `/{proposal_id}` (DELETE), `/scan` | Authenticated; proposal confirmation is explicit; legacy `/scan` returns extraction data only |
| Text AI capture | `/api/v1/capture/text/proposals`, `/{proposal_id}`, `/{proposal_id}/confirm`, `/{proposal_id}` (DELETE) | Authenticated; proposal creation does not persist a financial record; confirmation is explicit |

The database seeds `user` and `admin`, but the public authenticated principal contains only user ID, email, and preferred currency. There are no administrator-only routes, premium entitlement/subscription model, or server-side premium checks. Receipt and text AI routes therefore accept any authenticated account. The repository has no current product contract or data model from which to safely decide premium entitlement. Treat premium/admin enforcement as not implemented and a release blocker if the presentation promises those tiers; do not claim that hiding a client control enforces access.

## Financial and AI integrity findings

- Manual expenses, incomes, and transfers use the authenticated owner and validate their account/category references. Existing integration coverage exercises persistence and cross-owner restrictions.
- Dashboard and financial transaction queries exclude voided transactions where relevant; the schema restricts transaction types and statuses.
- Receipt confirmation already uses one expense unit of work to create the financial transaction and mark the proposal confirmed. It rejects non-confirmable/discarded/expired proposals and supports replay by transaction ID.
- Text AI confirmation previously opened a proposal transaction, then called independently committing financial services. A crash between their commits could leave the proposal open after the financial record was saved, allowing a retry to create a duplicate. The new internal proposal key plus database uniqueness makes these retries idempotent across expense, income, and transfer operations. Replays return the persisted operation’s values even when the retried command has a different amount/date. Fault injection after the financial commit proves retry leaves one transaction and one balance change for each intent.
- Confirmation values still pass through the same deterministic application services as manual entry; the model does not directly write financial records.

## Verification

| Check | Result |
|---|---|
| Full unit, API, property, and PostgreSQL integration suite | 413 passed; 0 skipped; 47 PostgreSQL integration tests ran |
| Total branch coverage | 89% |
| Ruff lint | Passed |
| Ruff format check | Passed; 221 files formatted |
| mypy | Passed; 140 source files |
| OpenAPI drift (`scripts/export_openapi.py --check`) | Passed; no public schema change from the internal idempotency key |
| `pip-audit` | No known vulnerabilities; local project package was skipped because it is not published on PyPI |
| Isolated test API | `/health/ready` returned 200 on `127.0.0.1:8016` |

The full suite reports non-failing SQLite `ResourceWarning`s from existing test code. The isolated service on port 8016 was restarted from the updated source and `/health/live` plus `/health/ready` both returned 200. The full suite used only `FINANCE_AI_TEST_DATABASE_URL`, pointed to the disposable database whose name ends in `_test`; the real backend database was not changed.

Commands run:

```bash
FINANCE_AI_TEST_DATABASE_URL=<private-isolated-test-database-url> uv run pytest --cov=finance_ai --cov-report=term:skip-covered -q
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run mypy src
uv run python scripts/export_openapi.py --check
uv run pip-audit
```

## Changed files

- `src/finance_ai/capture/service.py`
- `src/finance_ai/core/errors.py`
- `src/finance_ai/db/models/transactions.py`
- `src/finance_ai/db/repositories/expenses.py`
- `src/finance_ai/db/repositories/registration.py`
- `src/finance_ai/expenses/repositories.py`
- `src/finance_ai/expenses/service.py`
- `src/finance_ai/incomes/repository.py`
- `src/finance_ai/incomes/service.py`
- `src/finance_ai/transfers/repository.py`
- `src/finance_ai/transfers/service.py`
- `migrations/versions/0017_text_capture_financial_idempotency.py`
- `tests/capture_support.py`
- `tests/integration/test_income_persistence.py`
- `tests/integration/test_registration_persistence.py`
- `tests/integration/test_text_capture_persistence.py`
- `tests/test_error_handling.py`

No commit, push, or merge was performed. For physical UI validation, the isolated test API is running at `http://127.0.0.1:8016`; its private environment file is `/tmp/finance-audit-test-env.json` with mode `0600`. The integration fixtures drop/recreate the test schema, so do not run the integration suite while the physical UI session is using that API.

## Follow-up verification — 2026-10-16

The income amount boundary now rejects values above `9999999999999999.99` during request validation, before PostgreSQL can raise a numeric overflow. The dashboard flow totals and income averages now include confirmed income movements from the canonical financial transaction model as well as legacy income rows. A regression verifies the exact stored calendar day in the `America/Bogota` request and both storage paths.

The refreshed isolated full suite passes 415 tests (0 skipped), with 89% branch coverage. Ruff, format, mypy, OpenAPI drift, and `git diff --check` pass. The updated API was restarted on `127.0.0.1:8016` without a migration or reset of its existing database. Live black-box verification using a pre-existing synthetic account showed dashboard income `4500.00` for `2026-10-16`, oversized income returns 422, and deleting the synthetic income returns the dashboard total to `0.00`.

History date filters use inclusive UTC calendar-day bounds for confirmed transactions. This matches the date-only income and transfer contracts, which store their selected day as UTC midnight and return that stored UTC date. In the Flutter UI, converting that timestamp to Bogota local time renders the previous evening (`19:00`); the UI should display the API's date-only value for income/transfer instead of localizing the timestamp. Expense timestamps can retain timezone-aware display semantics.

## PR review follow-up — 2026-10-17

The review on PR #24 identified two valid retry cases in AI capture confirmation:

- A financial operation could commit before the proposal update failed; retrying would return the existing record with `replayed=false`, producing a misleading HTTP 201.
- A retry with an edited intent could search only for the new transaction type and collide with the per-user idempotency key for the original operation.

The confirmation path now resolves a confirmed operation by `(owner_id, proposal_id)` before dispatching by intent, reconstructs the original expense, income, or transfer, and marks recovered records as replays. Expense, income, and transfer creation paths also report whether an idempotent result was newly created or reused. Regression coverage checks recovery after proposal-update failure, changed-intent retry, and replay flags for expense, income, and transfer.

Validation on 2026-10-17: focused unit tests pass (37/37); Ruff and mypy pass. PostgreSQL integration tests were not run locally because `FINANCE_AI_TEST_DATABASE_URL` is unset; PR CI must validate those database-backed cases. No deployment, merge, or production database operation was performed.

## Backend dependency, workflow and license audit closure — 2026-10-19

### Scope and workspace

- Base: origin/develop at fe6b8f2 when the exclusive worktree was created.
- Branch: codex/backend-dependency-fixes.
- Worktree: /home/d3ad/codigos/finance-ai-backend-dependency-audit.
- No commit, push, merge, pull request, or real deployment was performed. uv.lock was not changed.

### Changes and verification

- .github/workflows/deploy.yml deploys only main to the currently shared finance-api service. A workflow_run must be a successful push CI run for the same repository and main; checkout and image tag use that run's head_sha. Manual dispatch is restricted to the main branch and queries Backend CI (ci.yml) for a successful push run whose head_sha exactly equals SOURCE_SHA (github.sha). It fails closed before migrations, image build, or GCP authentication when no such run exists. The workflow checks out/builds/migrates before GCP authentication. The verification query uses gh api --method GET; repository and SHA values are passed through environment variables, not interpolated into shell source.
- tests/property/test_registration_properties.py now checks the serialized response structure and exact field values, and checks the generated password against values rather than substrings. This preserves the no-password-echo property without the prior false positive where password currency matched preferred_currency.
- uv sync --all-groups --frozen: PASS. uv run pip-audit: PASS, no known vulnerabilities reported; the local project itself is not an index package and is skipped by pip-audit.
- Full suite with an isolated ephemeral PostgreSQL 18.6 container on 127.0.0.1:55439: **416 passed, 89% coverage**. The container had no persistent volume and was removed after the run. Ruff format check and lint on the changed property test: PASS.
- Workflow YAML parsing, dispatch/push/PR gate scenarios, bash -n, and a local gh mock returning a successful exact-SHA CI run: PASS. git diff --check: PASS.

### Deployment boundary and remaining operational checks

The repository's deploy workflow currently targets the same project finance-ai-api, region southamerica-east1, Cloud Run service finance-api, Artifact Registry path, and FINANCE_AI_DATABASE_URL secret from either branch workflow. That secret is used for Alembic migrations and seeds. To prevent develop from changing production while these resources are shared, the workflow now blocks develop until a distinct staging service, database, and secrets are configured. No deployment was attempted.

The workflow source was inspected locally, but GitHub repository settings were not available for verification. Branch protection/rulesets and the configured Google OIDC provider/trust conditions remain externally unverified. The workflow currently declares id-token: write at workflow scope; consider restricting it to the deploy job as an additional least-privilege follow-up.

### Runtime distribution license inventory (44)

Licenses below come from installed distribution METADATA fields (License-Expression or legacy License) unless a file path is shown. Runtime closure was determined with uv tree --format json --no-dev, matching the Docker uv sync --no-dev install. finance-ai-backend is the local project distribution.

| Distribution | License / evidence |
|---|---|
| alembic@1.19.1 | MIT — METADATA: License-Expression |
| annotated-doc@0.0.5 | MIT — METADATA: License-Expression |
| annotated-types@0.8.0 | MIT — METADATA: License-Expression |
| anyio@4.14.2 | MIT — METADATA: License-Expression |
| argon2-cffi@25.1.0 | MIT — METADATA: License-Expression |
| argon2-cffi-bindings@26.1.0 | MIT — METADATA: License-Expression |
| certifi@2026.7.22 | MPL-2.0 — METADATA: License |
| cffi@2.1.1 | MIT-0 — METADATA: License-Expression |
| click@8.5.0 | BSD-3-Clause — METADATA: License-Expression |
| Deprecated@1.3.1 | MIT — METADATA: License |
| dnspython@2.8.0 | ISC — METADATA: License |
| email-validator@2.3.0 | Unlicense — METADATA: License |
| fastapi@0.140.13 | MIT — METADATA: License-Expression |
| finance-ai-backend@0.1.0 | UNKNOWN — no license declaration in pyproject.toml, installed METADATA, or distribution license file; only unresolved entry |
| greenlet@3.5.5 | MIT AND PSF-2.0 — METADATA: License-Expression |
| h11@0.16.0 | MIT — METADATA: License |
| httpcore@1.0.9 | BSD-3-Clause — METADATA: License-Expression |
| httptools@0.8.0 | MIT — METADATA: License-Expression |
| httpx@0.28.1 | BSD-3-Clause — METADATA: License |
| idna@3.19 | BSD-3-Clause — METADATA: License-Expression |
| limits@5.8.0 | MIT — METADATA: License-Expression |
| Mako@1.4.1 | MIT — METADATA: License-Expression |
| MarkupSafe@3.0.3 | BSD-3-Clause — METADATA: License-Expression |
| packaging@26.3 | Apache-2.0 OR BSD-2-Clause — METADATA: License-Expression |
| pillow@12.3.0 | MIT-CMU — METADATA: License-Expression |
| pillow-heif@1.8.0 | BSD-3-Clause — METADATA: License; bundled native components listed below |
| psycopg@3.3.4 | LGPL-3.0-only — METADATA: License-Expression |
| psycopg-binary@3.3.4 | LGPL-3.0-only — METADATA: License-Expression |
| pycparser@3.0 | BSD-3-Clause — METADATA: License-Expression |
| pydantic@2.13.5 | MIT — METADATA: License-Expression |
| pydantic-core@2.46.5 | MIT — METADATA: License-Expression |
| pydantic-settings@2.15.0 | MIT — METADATA: License-Expression |
| python-dotenv@1.2.3 | BSD-3-Clause — METADATA: License |
| python-multipart@0.0.32 | Apache-2.0 — METADATA: License-Expression |
| PyYAML@6.0.3 | MIT — METADATA: License |
| SQLAlchemy@2.0.52 | MIT — METADATA: License |
| starlette@1.6.0 | BSD-3-Clause — METADATA: License-Expression |
| typing_extensions@4.16.0 | PSF-2.0 — METADATA: License-Expression |
| typing-inspection@0.4.4 | MIT — METADATA: License-Expression |
| uvicorn@0.52.4 | BSD-3-Clause — METADATA: License-Expression |
| uvloop@0.22.1 | MIT License — METADATA: License |
| watchfiles@1.2.0 | MIT — METADATA: License-Expression |
| websockets@17.1 | BSD-3-Clause — METADATA: License-Expression |
| wrapt@2.3.0 | BSD-2-Clause — METADATA: License-Expression |

### Dev/test-only distribution license inventory (39)

This is the all-groups installed distribution set minus the runtime closure above. Evidence is from METADATA unless a license file is shown.

| Distribution | License / evidence |
|---|---|
| boolean.py@5.0 | BSD-2-Clause — METADATA: License |
| CacheControl@0.14.4 | Apache-2.0 — METADATA: License-Expression |
| charset-normalizer@3.5.1 | MIT — METADATA: License |
| coverage@7.16.0 | Apache-2.0 — METADATA: License |
| cyclonedx-python-lib@11.12.0 | Apache-2.0 — METADATA: License |
| defusedxml@0.7.1 | PSFL — METADATA: License |
| filelock@3.32.4 | MIT — METADATA: License-Expression |
| httpcore2@2.12.0 | BSD-3-Clause — METADATA: License-Expression |
| httpx2@2.12.0 | BSD-3-Clause — METADATA: License-Expression |
| hypothesis@6.165.10 | MPL-2.0 — METADATA: License-Expression |
| iniconfig@2.3.0 | MIT — METADATA: License-Expression |
| librt@0.15.0 | MIT — METADATA: License-Expression |
| license-expression@30.4.4 | Apache-2.0 — METADATA: License |
| markdown-it-py@4.2.0 | MIT — markdown_it_py-4.2.0.dist-info/licenses/LICENSE and LICENSE.markdown-it |
| mdurl@0.1.2 | MIT — mdurl-0.1.2.dist-info/LICENSE |
| msgpack@1.2.2 | Apache-2.0 — METADATA: License-Expression |
| mypy@1.20.2 | MIT — METADATA: License-Expression |
| mypy_extensions@1.1.0 | MIT — METADATA: License-Expression |
| packageurl-python@0.17.6 | MIT — METADATA: License |
| pathspec@1.1.1 | MPL-2.0 — pathspec-1.1.1.dist-info/licenses/LICENSE |
| pip@26.2.1 | MIT — METADATA: License-Expression |
| pip-api@0.0.34 | Apache-2.0 — pip_api-0.0.34.dist-info/LICENSE |
| pip_audit@2.10.1 | Apache-2.0 — pip_audit-2.10.1.dist-info/licenses/LICENSE |
| pip-requirements-parser@32.0.1 | MIT — METADATA: License |
| platformdirs@4.11.5 | MIT — METADATA: License-Expression |
| pluggy@1.6.0 | MIT — METADATA: License |
| py-serializable@2.1.0 | Apache-2.0 — METADATA: License |
| Pygments@2.21.0 | BSD-2-Clause — METADATA: License-Expression |
| pyparsing@3.3.2 | MIT — METADATA: License-Expression |
| pytest@9.1.1 | MIT — METADATA: License-Expression |
| pytest-cov@7.1.0 | MIT — METADATA: License-Expression |
| requests@2.34.2 | Apache-2.0 — METADATA: License |
| rich@15.0.0 | MIT — METADATA: License |
| ruff@0.16.5 | MIT — METADATA: License-Expression |
| sortedcontainers@2.4.0 | Apache 2.0 — METADATA: License |
| tomli@2.4.1 | MIT — METADATA: License-Expression |
| tomli_w@1.2.0 | MIT — tomli_w-1.2.0.dist-info/LICENSE |
| truststore@0.10.4 | MIT — METADATA: License-Expression |
| urllib3@2.8.0 | MIT — METADATA: License-Expression |

### Native components and license obligations

The selected Linux x86_64 pillow-heif wheel includes these loaded libraries (confirmed from the installed wheel and ldd):

| Bundled component | Version | License / evidence |
|---|---|---|
| libheif | 1.23.4 | LGPLv3 — pillow_heif-1.8.0.dist-info/licenses/LICENSES_bundled.txt |
| libde265 | 1.1.3 | LGPLv3 — pillow_heif-1.8.0.dist-info/licenses/LICENSES_bundled.txt |
| x265 | 4.2 | GPLv2 — pillow_heif-1.8.0.dist-info/licenses/LICENSES_bundled.txt |
| MinGW-w64 runtime | Windows wheels only | GPL-3.0-with-GCC-exception, MIT, and BSD components — same bundled-license file; not present in the selected Linux wheel |

The direct pillow-heif source distribution is BSD-3-Clause, while its binary wheels identify GPLv2 due to bundled base-library licenses. pyproject.toml requests psycopg[binary], which selects the psycopg-binary extra and its LGPL-3.0-only distribution. uvicorn[standard] selects the standard extras including httptools, uvloop, watchfiles, and websockets. The application redistributes these runtime binaries in its Docker image. LGPL/GPL notice, source, relinking, and corresponding distribution obligations should be assessed for the actual distribution model; this inventory does not establish non-compliance. No license metadata or lockfile was changed in this audit.

## Runtime native wheel/component inventory — 2026-10-19

This read-only supplement expands the Python distribution inventory above; it does not replace it or change source code or uv.lock. The checkout's existing .venv contained unrelated distributions and package versions that did not match uv.lock. To avoid attributing those artifacts to this backend, the native audit exported the frozen runtime requirements and installed that exact set into the temporary directory /tmp/finance-ai-runtime-site (43 locked third-party runtime distributions; project omitted from export). Files were mapped to distributions using wheel dist-info/RECORD. ELF dynamic dependencies were checked with ldd. No repository file or lockfile was changed.

| Runtime distribution | Native extensions in selected Linux x86_64 wheels | Wheel-private shared libraries observed | License evidence and limits |
|---|---|---|---|
| argon2-cffi-bindings@26.1.0 | _argon2_cffi_bindings/_ffi.abi3.so | None found | Package metadata MIT; wheel has dist-info/licenses/LICENSE. ldd reports host libc and pthread. |
| cffi@2.1.1 | _cffi_backend.cpython-314-x86_64-linux-gnu.so | None found | Package metadata MIT-0; wheel has dist-info/licenses/LICENSE. ldd reports host libc and pthread. |
| greenlet@3.5.5 | greenlet/_greenlet…so plus two greenlet/tests extension modules | None found | Package metadata MIT AND PSF-2.0; wheel has LICENSE and LICENSE.PSF. Test extension artifacts are present in the installed wheel. |
| httptools@0.8.0 | httptools/parser/parser…so; url_parser…so | None found | Package metadata MIT; wheel has dist-info/licenses/LICENSE. ldd reports host libc and pthread. |
| MarkupSafe@3.0.3 | markupsafe/_speedups…so | None found | Package metadata BSD-3-Clause; wheel has dist-info/licenses/LICENSE.txt. |
| pillow@12.3.0 | 8 PIL extension modules: _avif, _imaging, _imagingcms, _imagingft, _imagingmath, _imagingmorph, _imagingtk, _webp | 18 files under pillow.libs: libXau, libavif, libbrotlicommon, libbrotlidec, libfreetype, libharfbuzz, libjpeg, liblcms2, liblzma, libopenjp2, libpng16, libsharpyuv, libtiff, libwebp, libwebpdemux, libwebpmux, libxcb, libzstd | Pillow metadata MIT-CMU; bundled wheel dist-info/licenses/LICENSE contains Pillow and third-party license notices. The file is a multi-license notice bundle, not a concise per-ELF SPDX mapping; individual bundled-library license mapping is not asserted here. ldd confirms the PIL modules load several pillow.libs objects; other dependencies include host libc, libdl, libgcc_s, libm, libpthread, libstdc++, and zlib. |
| pillow-heif@1.8.0 | _pillow_heif…so | 3 files under pillow_heif.libs: libheif 1.23.4, libde265 1.1.3, libx265 4.2 | Package metadata/source license BSD-3-Clause. dist-info/licenses/LICENSES_bundled.txt explicitly assigns LGPLv3 to libheif/libde265 and GPLv2 to x265; ldd confirms all three are loaded by the extension. The same notice lists MinGW-w64 GPL-3.0-with-GCC-exception/MIT/BSD runtime components for Windows wheels only; those DLLs are not part of this selected Linux wheel. |
| psycopg-binary@3.3.4 | psycopg_binary/_psycopg…so and pq…so | 15 files under psycopg_binary.libs: libcom_err, libcrypt, libcrypto, libgssapi_krb5, libk5crypto, libkeyutils, libkrb5, libkrb5support, liblber, libldap, libpcre, libpq, libsasl2, libselinux, libssl | Wheel metadata and dist-info/licenses/LICENSE.txt document LGPL-3.0-only for psycopg-binary. ldd confirms the extensions load the listed wheel-private libraries; host libc, libdl, libm, libpthread, libresolv, and zlib are also used. The inspected wheel has no per-component license manifest for the 15 bundled libraries, so their individual licenses are not assigned in this inventory. |
| pydantic-core@2.46.5 | pydantic_core/_pydantic_core…so | None found | Package metadata MIT; wheel includes dist-info/licenses/LICENSE. ldd reports host libc, libdl, libgcc_s, libpthread, and librt. |
| PyYAML@6.0.3 | yaml/_yaml…so | None found | Package metadata MIT; wheel includes dist-info/licenses/LICENSE. |
| SQLAlchemy@2.0.52 | 5 sqlalchemy/cyextension modules: collections, immutabledict, processors, resultproxy, util | None found | Package metadata MIT; wheel includes dist-info/licenses/LICENSE. ldd reports host libc and pthread. |
| uvloop@0.22.1 | uvloop/loop…so | None found | Package metadata MIT License; wheel includes LICENSE-MIT and LICENSE-APACHE. ldd reports host libc, pthread, and librt. |
| watchfiles@1.2.0 | watchfiles/_rust_notify…so | None found | Package metadata MIT; wheel includes dist-info/licenses/LICENSE. ldd reports host libc, libdl, libgcc_s, pthread, and librt. |
| websockets@17.1 | websockets/speedups…so | None found | Package metadata BSD-3-Clause; wheel includes dist-info/licenses/LICENSE. |
| wrapt@2.3.0 | wrapt/_wrappers…so | None found | Package metadata BSD-2-Clause; wheel includes dist-info/licenses/LICENSE. |

Across the mapped runtime wheel files, 30 extension modules and 36 wheel-private shared-library files were observed (including the greenlet test extensions). “None found” means ldd did not resolve a dependency to another file inside the isolated wheel target; it does not prove that an extension contains no statically linked code. Host libraries reported by ldd are supplied by the container operating system, not by these Python wheels, and were not license-audited here. Source and wheel artifacts may differ; the evidence above describes the selected CPython 3.14 Linux x86_64 wheels, not every platform in uv.lock. Pillow's license bundle and psycopg-binary's bundled-library set require per-component source/license evidence before making further component-level license assertions. This supplement records evidence and gaps; it makes no finding of non-compliance.

## Exact-lock PostgreSQL suite confirmation — 2026-10-19

The earlier 416-test result could not be tied conclusively to the then-current .venv after the later native inventory found unrelated distributions and version drift in that environment. It is superseded by this isolated rerun. A fresh environment at /tmp/finance-ai-backend-audit-verify-venv was created using UV_PROJECT_ENVIRONMENT and synchronized with uv sync --all-groups --frozen; uv sync --check subsequently reported 83 packages checked and no changes required. A version spot-check matched locked SQLAlchemy 2.0.52, FastAPI 0.140.13, psycopg-binary 3.3.4, pytest 9.1.1, and pip-audit 2.10.1.

The full command was UV_PROJECT_ENVIRONMENT=/tmp/finance-ai-backend-audit-verify-venv FINANCE_AI_ENVIRONMENT=test FINANCE_AI_DATABASE_URL=<temporary-local-test-url> FINANCE_AI_TEST_DATABASE_URL=<temporary-local-test-url> uv run --frozen pytest --cov=finance_ai --cov-report=term:skip-covered. It completed with **416 passed, 89% coverage**. PostgreSQL 18.6 ran in a newly created container without volumes or bind mounts, published only on 127.0.0.1:55440, using the isolated finance_ai_audit_test database. An initial harness attempt used a database name without the required _test suffix and was rejected by the integration fixture before test setup; it did not touch another database. The corrected run passed. The temporary container and exact-lock environment were removed after verification. No other containers or databases were changed.
