-- ════════════════════════════════════════════════════════════
-- Q08 — Internal Collections IT (two result sets in one file)
--
-- Result sets:
--   A_SEGMENT — one row per product × prev_delinquency_bucket
--               × prev_delinquency_segment × transaction_month
--   B_TOTAL   — one row per product × transaction_month (whole IT book)
--
-- KPI definitions (handover Section 2 — locked):
--   IT Collection Yield = net_receipts / (opening_arrears + instalment_due) × 100
--     Population: A_SEGMENT, delinquency_bucket IN (Early Arrears, Deep Arrears)
--   IT Payer Rate       = payers / loan_count × 100   (same population)
--   Total Collections   = SUM(net_receipts) from B_TOTAL across all products
--
-- FTTC split (handover Section 7.2 pending item — now addressed below):
--   instalment_collections   txn_category = 'Instalment Receipt'  (auto / DebiCheck)
--   effort_collections       txn_category = 'Other Receipt'       (agent intervention)
--   effort_pct               effort_collections / net_receipts × 100
--   instalment_pct           instalment_collections / net_receipts × 100
--   The split is joined at loan grain from FTTC and aggregated alongside the
--   primary net_receipts roll-up so existing yield/payer logic is unchanged.
-- ════════════════════════════════════════════════════════════


WITH params AS (SELECT DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month' AS reporting_month),

     -- ── Collections split from FTTC ──────────────────────────────
     -- One row per loan per month with the instalment vs effort breakdown.
     fttc_collections AS (SELECT fttc.loannumber,
                                 DATE_TRUNC('month', fttc.txn_date)::date AS transaction_month,

                                 SUM(fttc.txn_amount) FILTER (
                                     WHERE fttc.txn_category = 'Instalment Receipt'
                                     ) * -1                               AS instalment_collections,

                                 SUM(fttc.txn_amount) FILTER (
                                     WHERE fttc.txn_category = 'Other Receipt'
                                     ) * -1                               AS effort_collections

                          FROM fasta_views.financialtransactions_transactions_corrected fttc
                                   JOIN params p
                                        ON DATE_TRUNC('month', fttc.txn_date)::date
                                            BETWEEN (SELECT reporting_month FROM params) - INTERVAL '12 months'
                                            AND (SELECT reporting_month FROM params)
                          WHERE fttc.txn_group = 'Net Receipts'
                            AND fttc.txn_category IN ('Instalment Receipt', 'Other Receipt')
                          GROUP BY 1, 2),

     it_base AS (SELECT mds.loannumber,
                        mds.transaction_month,
                        pt.product,
                        mds.prev_delinquency_bucket,
                        mds.prev_delinquency_segment,
                        ptn.net_receipts,
                        mds.opening_arrears,
                        ptn.arrears,
                        ptn.opening_balance,
                        ptn.closing_balance,
                        ptn.instalment_due_in_month,
                        lag(mds.consecutive_misses)
                        over (partition by mds.loannumber order by mds.transaction_month) as opening_consecutive_misses,
                        lag(mds.months_past_maturity)
                        over (partition by mds.loannumber order by mds.transaction_month) as opening_months_past_maturity,
                        mds.statement_number,
                        mds.term,

                        -- FTTC split joined at loan grain
                        COALESCE(fc.instalment_collections, 0)              AS instalment_collections,
                        COALESCE(fc.effort_collections, 0)                  AS effort_collections
                 FROM fasta_views.mv_delinquency_segments mds
                     JOIN fasta_views.mv_department_start_of_month mdsm
                               ON mds.loannumber = mdsm.loannumber
                                   AND mds.transaction_month = mdsm.reporting_month
                          JOIN fasta_views.payments_table_full ptn
                               ON mds.loannumber = ptn.loannumber
                                   AND mds.transaction_month = ptn.transaction_month
                          JOIN fasta_views.mv_commercial_loan_product_type pt
                               ON mds.loannumber = pt.loannumber
                          JOIN fasta_views.mv_commercial_opening_activity act
                               ON act.loannumber = mds.loannumber
                                   AND act.transaction_month = mds.transaction_month
                                   AND act.opening_in_term_flag = TRUE
                          LEFT JOIN fttc_collections fc
                               ON fc.loannumber = mds.loannumber
                                   AND fc.transaction_month = mds.transaction_month::date
                 WHERE mds.transaction_month::date BETWEEN
                         (SELECT reporting_month FROM params) - INTERVAL '12 months'
                     AND (SELECT reporting_month FROM params)
                   AND mds.opening_is_active IN (1, 2, 3, 9999)
                 AND (mdsm.department_start_of_month in ('Internal Collections - in term') or
                              mdsm.touched_it_in_month)
                 ),

-- Result set A: segmented by delinquency bucket
     result_a AS (SELECT 'A_SEGMENT'                                       AS result_set,
                         transaction_month::date                           AS transaction_month,
                         product,
                         prev_delinquency_bucket                           as delinquency_bucket,
                         prev_delinquency_segment                          as delinquency_segment,
                         COUNT(DISTINCT loannumber)                        AS loan_count,
                         ROUND(SUM(net_receipts), 2)                       AS net_receipts,
                         COUNT(loannumber) FILTER (WHERE net_receipts > 0) AS payers,
                         ROUND(SUM(opening_arrears), 2)                    AS opening_arrears,
                         ROUND(SUM(arrears), 2)                            AS arrears,
                         ROUND(SUM(opening_balance), 2)                    AS opening_balance,
                         ROUND(SUM(closing_balance), 2)                    AS closing_balance,
                         ROUND(SUM(instalment_due_in_month), 2)            AS instalment_due,
                         ROUND(SUM(net_receipts) / NULLIF(SUM(instalment_due_in_month + opening_arrears), 0) * 100, 2)
                                                                           AS collection_yield_pct,
                         ROUND(SUM(net_receipts) / NULLIF(SUM(opening_balance), 0) * 100, 2)
                                                                           AS balance_yield_pct,

                         -- FTTC split — auto vs effort composition
                         ROUND(SUM(instalment_collections), 2)             AS instalment_collections,
                         ROUND(SUM(effort_collections), 2)                 AS effort_collections,
                         ROUND(SUM(instalment_collections)
                                   / NULLIF(SUM(net_receipts), 0) * 100, 2) AS instalment_pct,
                         ROUND(SUM(effort_collections)
                                   / NULLIF(SUM(net_receipts), 0) * 100, 2) AS effort_pct
                  FROM it_base
                  GROUP BY 1, 2, 3, 4, 5),

-- Result set B: total commercial book monthly view
     result_b AS (SELECT 'B_TOTAL'                                         AS result_set,
                         transaction_month::date                           AS transaction_month,
                         product,
                         NULL::text                                        AS delinquency_bucket,
                         NULL::text                                        AS delinquency_segment,
                         COUNT(DISTINCT loannumber)                        AS loan_count,
                         ROUND(SUM(net_receipts), 2)                       AS net_receipts,
                         COUNT(loannumber) FILTER (WHERE net_receipts > 0) AS payers,
                         ROUND(SUM(opening_arrears), 2)                    AS opening_arrears,
                         ROUND(SUM(arrears), 2)                            AS arrears,
                         ROUND(SUM(opening_balance), 2)                    AS opening_balance,
                         ROUND(SUM(closing_balance), 2)                    AS closing_balance,
                         ROUND(SUM(instalment_due_in_month), 2)            AS instalment_due,
                         ROUND(SUM(net_receipts) / NULLIF(SUM(instalment_due_in_month + opening_arrears), 0) * 100, 2)
                                                                           AS collection_yield_pct,
                         ROUND(SUM(net_receipts) / NULLIF(SUM(opening_balance), 0) * 100, 2)
                                                                           AS balance_yield_pct,

                         -- FTTC split — auto vs effort composition
                         ROUND(SUM(instalment_collections), 2)             AS instalment_collections,
                         ROUND(SUM(effort_collections), 2)                 AS effort_collections,
                         ROUND(SUM(instalment_collections)
                                   / NULLIF(SUM(net_receipts), 0) * 100, 2) AS instalment_pct,
                         ROUND(SUM(effort_collections)
                                   / NULLIF(SUM(net_receipts), 0) * 100, 2) AS effort_pct
                  FROM it_base
                  GROUP BY 1, 2, 3)

SELECT *
FROM result_a
UNION ALL
SELECT *

FROM result_b
ORDER BY result_set, transaction_month DESC, product, delinquency_bucket;
