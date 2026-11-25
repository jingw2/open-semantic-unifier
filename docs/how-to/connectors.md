# Connector support and validation

The legacy data-source connector module recognizes DuckDB, SQLite, MySQL, PostgreSQL, and MongoDB. Support here means a code path exists; it is not a claim that every version, authentication mode, or deployment topology has been integration-tested.

| Connector | Driver path | Validation in this project | Release wording |
| --- | --- | --- | --- |
| SQLite | Python sqlite3 | Automated connection, read, and sink tests | Tested locally |
| PostgreSQL | psycopg2 | Compose startup, migration, API and worker lifecycle; opt-in live connector test | Tested in Compose and live |
| DuckDB | duckdb | Automated connection, read, and sink tests | Tested locally |
| MySQL | PyMySQL | Opt-in live connector test | Tested live |
| MongoDB | PyMongo | Opt-in live connector test | Tested live |

Use the database connection check before configuring a source. The current bulk identity-ingest contract is file/API based; database source support in the legacy profiling and extraction routes does not mean every connector is covered by the new versioned unification workflow.
