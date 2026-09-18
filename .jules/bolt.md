# Bolt's Journal

## 2025-05-18 - Batch category reordering with SQLAlchemy CASE expression
**Learning:** Updating sort order in a loop issuing individual UPDATE statements executes N separate database queries. In SQLAlchemy 2.0, using `case()` with `where(Category.id.in_(category_ids))` condenses N update statements into a single SQL query. Note that `session.execute(update(Model), mappings)` fails when custom `where` clauses are used without exact primary key mappings.
**Action:** Use `case()` dictionary mapping with `.where(Model.id.in_(ids))` to batch positional/sequence updates in SQLAlchemy repositories in 1 query.
