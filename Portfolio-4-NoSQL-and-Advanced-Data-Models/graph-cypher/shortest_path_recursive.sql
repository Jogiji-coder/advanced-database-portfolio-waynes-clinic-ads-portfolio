-- =====================================================================
-- G5 in SQL: shortest chain of shared clinicians between two patients.
-- Breadth-first search with WITH RECURSIVE over an undirected edge list
-- built from completed appointments (Aug-Oct 2026). It returns only the
-- distance; reconstructing the actual chain would need path arrays and
-- cycle checks, which make the working set explode. Cypher: one line.
-- =====================================================================
WITH RECURSIVE
visits AS (
    SELECT DISTINCT patient_id, doctor_id
    FROM clinic.appointment
    WHERE status = 'Completed' AND scheduled_start >= '2026-08-01' AND scheduled_start < '2026-10-28'),
edges AS (
    SELECT 'P' || patient_id AS src, 'D' || doctor_id AS dst FROM visits
    UNION ALL
    SELECT 'D' || doctor_id, 'P' || patient_id FROM visits),
bfs (node, depth) AS (
    SELECT 'P3'::text, 0                       -- Tyrone Jackson
    UNION                                      -- UNION (not ALL) de-duplicates each level
    SELECT e.dst, b.depth + 1
    FROM bfs b JOIN edges e ON e.src = b.node
    WHERE b.depth < 4)
SELECT min(depth) AS hops
FROM bfs
WHERE node = 'P16';                            -- Zara Ahmed
