# Advanced Database Systems Portfolio — Wayne's Clinic Baltimore


One hypothetical organisation, five database disciplines. Wayne's Clinic Baltimore is a fictional 10-department outpatient and urgent-care hospital. Its database stores **patients, doctors, appointments, diagnoses, prescriptions and laboratory records**, and supports three core operations: **book an appointment, store a diagnosis, retrieve patient history**. Each portfolio takes the same case study one level further, and every claim is backed by scripts that run and by the evidence they produced.

| # | Portfolio | Highlights | Folder |
|---|---|---|---|
| 1 | **Database Design and Modelling** | Conceptual + logical Crow's Foot ERD generated from the catalogue; 16-table BCNF/4NF schema; `EXCLUDE` constraints that make double booking impossible; triggers for allergy safety; **28/28 constraint tests pass** | [Portfolio-1-Database-Design-and-Modelling](Portfolio-1-Database-Design-and-Modelling/) |
| 2 | **Query Processing and Optimisation** | 2.6M-row synthetic dataset; 11 business queries; 9 measured experiments with 25 saved `EXPLAIN ANALYZE` plans; patient history **192× faster**, doctor's day list **1,148×**, dashboard **539×**; write-cost of every index measured | [Portfolio-2-Query-Processing-and-Optimisation](Portfolio-2-Query-Processing-and-Optimisation/) |
| 3 | **Transactions and Concurrency** | Commit / rollback / savepoint; every isolation anomaly reproduced with real concurrent sessions (lost update, write skew, phantom, double booking, deadlock) and the mechanism that prevents it; a real **crash-and-WAL-recovery** durability test | [Portfolio-3-Transactions-and-Concurrency](Portfolio-3-Transactions-and-Concurrency/) |
| 4 | **NoSQL and Advanced Data Models** | Same clinic in **MongoDB 8** (641k schema-validated documents, multi-document transactions), **Redis** (slot holds, triage queue, alert streams), a **property graph** in Cypher (contact tracing, drug interactions, shortest paths), **Parquet/DuckDB** columnar analytics and a **Cassandra** wide-column design; suitability analysis | [Portfolio-4-NoSQL-and-Advanced-Data-Models](Portfolio-4-NoSQL-and-Advanced-Data-Models/) |
| 5 | **Distributed and Cloud Database** | Six local PostgreSQL servers: synchronous + asynchronous replication, stale reads, the CAP trade-off, crash failover with **RPO = 0** and `pg_rewind` rejoin; hash sharding with `postgres_fdw`; MongoDB automatic election and hashed sharding; HIPAA-aware AWS design in Terraform | [Portfolio-5-Distributed-and-Cloud-Database](Portfolio-5-Distributed-and-Cloud-Database/) |
| — | **Final documentation and reflection** | 2–3 page summary, evidence, reflection, references and AI-use declaration | [Final-Documentation](Final-Documentation/) |

Each portfolio folder contains a `README.md` (the write-up, rendered by GitHub), a Word/PDF copy of the same report, the SQL/scripts, and an `evidence/` folder of raw logs plus images rendered from them.

## Technology

PostgreSQL 16.15 · MongoDB 8.0.23 · Redis 7 · Kùzu 0.11 (Cypher) · DuckDB · Python 3.13 (psycopg 3, pymongo, redis-py) · Graphviz · Terraform (reference IaC). Everything except the AWS deployment was executed on Ubuntu 24.04 (2 vCPU, 8 GB RAM).

## Reproduce

```bash
# prerequisites: PostgreSQL 16 on :5432 (user postgres / password postgres), MongoDB 7+ as replica set rs0 on :27017,
# Redis on :6379, mongosh, Graphviz, Python packages:
pip install "psycopg[binary]" pymongo redis kuzu duckdb pyarrow matplotlib pillow
./run_all.sh            # or run any portfolio's run_portfolioN.sh on its own
```

Portfolio 3's crash test and Portfolio 5 restart database servers, so run them only on a test machine you control.

## Repository layout

```
Portfolio-1-.../   sql/  diagrams/  docs/  evidence/  README.md  run_portfolio1.sh
Portfolio-2-.../   sql/  scripts/  plans/  results/  charts/  evidence/  README.md
Portfolio-3-.../   sql/  scripts/  evidence/  README.md
Portfolio-4-.../   document-mongodb/  key-value-redis/  graph-cypher/  columnar-duckdb/  column-family-cassandra/  diagrams/  evidence/
Portfolio-5-.../   scripts/  cloud/{terraform,docker}  evidence/  README.md
Final-Documentation/   summary report (.docx, .pdf)
tools/render_terminal.py   renders captured console logs as the evidence images
```

All people, phone numbers and drug codes in the data are fictional. ICD-10-CM codes are real.

## Publish to GitHub

```bash
cd waynes-clinic-ads-portfolio
git init && git add . && git commit -m "Advanced Database Systems portfolio - Wayne's Clinic Baltimore"
git branch -M main
git remote add origin https://github.com/Jogiji-coder/waynes-clinic-ads-portfolio.git
git push -u origin main
```

Then replace `Jogiji` in this README and in `Final-Documentation/` with the real repository link.

