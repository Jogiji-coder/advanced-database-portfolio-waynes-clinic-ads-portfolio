# Benchmark summary

Median of 7 `EXPLAIN (ANALYZE, BUFFERS)` executions after one warm-up run (warm cache). PostgreSQL 16.15, 2 vCPU, shared_buffers 128MB, work_mem 4MB, JIT off. Run at 2026-10-28T22:44:39.

| Exp | Variant | Median ms | vs first | Buffers hit/read | Access path |
|---|---|---|---|---|---|
| E1 | Before: FK columns unindexed | 120.915 | — | 17142/12899 | Subquery Scan; Seq Scan on prescription; Seq Scan on appointment; Index Only Scan on doctor using doctor_pkey; Index Sca |
|  | After: 3 FK indexes | 0.630 | 191.9x faster | 446/0 | Subquery Scan; Bitmap Heap Scan on appointment; Bitmap Index Scan using ix_appt_patient_start; Seq Scan on doctor; Seq S |
| E2a | No index on last_name | 3.182 | — | 808/0 | Seq Scan on patient |
|  | Default B-tree (not usable for LIKE) | 3.154 | 1.0x faster | 808/0 | Seq Scan on patient |
|  | B-tree with varchar_pattern_ops | 0.431 | 7.4x faster | 526/0 | Bitmap Heap Scan on patient; Bitmap Index Scan using ix_patient_last_pattern |
| E2b | Before: B-tree cannot serve ILIKE '%...%' | 19.448 | — | 808/0 | Seq Scan on patient |
|  | After: GIN trigram index | 0.768 | 25.3x faster | 528/0 | Bitmap Heap Scan on patient; Bitmap Index Scan using ix_patient_last_trgm |
| E3 | Non-sargable date cast, no index | 28.700 | — | 4167/2345 | Seq Scan on appointment; Index Scan on patient using patient_pkey |
|  | Sargable range, no index | 29.118 | 1.0x slower | 5012/1481 | Seq Scan on appointment; Index Scan on patient using patient_pkey |
|  | Non-sargable + composite index | 2.936 | 9.8x faster | 2831/0 | Bitmap Heap Scan on appointment; Bitmap Index Scan using ix_appt_doctor_start; Index Scan on patient using patient_pkey |
|  | Sargable + composite index | 0.025 | 1148.0x faster | 30/0 | Index Scan on appointment using ix_appt_doctor_start; Index Scan on patient using patient_pkey |
| E4 | No index on resulted_at | 25.208 | — | 4157/0 | Seq Scan on lab_result; Index Scan on lab_order using lab_order_pkey; Index Scan on appointment using appointment_pkey;  |
|  | BRIN on resulted_at | 2.473 | 10.2x faster | 763/0 | Bitmap Heap Scan on lab_result; Bitmap Index Scan using ix_lab_result_resulted_brin; Index Scan on lab_order using lab_o |
|  | Partial B-tree WHERE flag = 'C' | 0.443 | 56.9x faster | 722/0 | Index Scan on lab_result using ix_lab_result_critical; Index Scan on lab_order using lab_order_pkey; Index Scan on appoi |
| E5 | Correlated COUNT(*) = 0 | 462.942 | — | 471306/10702 | Seq Scan on prescription; Seq Scan on medication; Bitmap Heap Scan on prescription_item; Bitmap Index Scan using ix_item |
|  | NOT IN (subquery) | 218.156 | 2.1x faster | 77101/2456 | Seq Scan on appointment; Seq Scan on lab_order; Bitmap Heap Scan on lab_result; Bitmap Index Scan using ix_lab_result_te |
|  | NOT EXISTS (anti-join) | 101.965 | 4.5x faster | 56252/0 | Seq Scan on lab_order; Bitmap Heap Scan on lab_result; Bitmap Index Scan using ix_lab_result_test; Index Scan on appoint |
| E6 | Live aggregation over 590k diagnoses | 222.480 | — | 4469/7085 | Subquery Scan; Seq Scan on diagnosis; Seq Scan on appointment; Seq Scan on doctor; Seq Scan on department |
|  | Query the materialised view | 0.413 | 538.7x faster | 8/0 | Subquery Scan; Seq Scan on mv_dept_dx_month |
| E7 | Planner's choice | 317.138 | — | 6381/5101 | Seq Scan on appointment; Seq Scan on diagnosis |
|  | Forced merge join | 290.956 | 1.1x faster | 12151/0 | Index Scan on appointment using appointment_pkey; Index Only Scan on diagnosis using ux_diagnosis_one_primary |
|  | Forced nested loop | 500.801 | 1.6x slower | 1833257/2432 | Seq Scan on diagnosis; Index Scan on appointment using appointment_pkey |
|  | Planner's choice, random_page_cost = 1.1 (SSD) | 321.658 | 1.0x slower | 7760/0 | Seq Scan on appointment; Index Only Scan on diagnosis using ux_diagnosis_one_primary |
| E8 | Composite index, heap lookups | 1.844 | — | 1380/0 | Bitmap Heap Scan on appointment; Bitmap Index Scan using ix_appt_doctor_start |
|  | Covering index INCLUDE (status) | 0.356 | 5.2x faster | 11/0 | Index Only Scan on appointment using ix_appt_doctor_start_cov |

**E9 — write cost (100 000-row insert, median of 3)**

| Configuration | Insert ms | Index size |
|---|---|---|
| Primary key only | 191 | 2.3 MB |
| + 3 B-tree indexes | 649 | 13.9 MB |
| + 3 B-tree + 2 GiST exclusion | 5484 | 28.0 MB |
