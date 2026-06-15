-- ============================================================
-- Query 7: OOT Collections — Out-of-Term Operational View
-- Deck: Operational View — Out-of-Term, Slide 7
-- Source: fasta_views.mv_delinquency_segments (mds)          ← base
--         fasta_views.mv_commercial_opening_activity (act)   ← gates ptn_flows
--         fasta_views.mv_commercial_loan_product_type (pt)
--         fasta_views.payments_table_full (ptn)               ← narrow CTE
--         fasta_views.provision_review_master_table (prmt)    ← bs_prov
--         fasta_views.financialtransactions_transactions_corrected (fttc)
-- Grain: One row per product × prev_mpm_band × mpm_band × transaction_month
-- Window: 13 months for MoM trend
--
-- Population gate:
--   ptn_flows filters to OOT-at-SOM via mv_commercial_opening_activity:
--     opening_is_active IN (1, 2, 9999)
--     opening_in_term_flag = FALSE
--   is_active = 2 includes debt review + written-off collectable accounts —
--   their receipts are commercially relevant (handover Section 6 / 9.1).
--
-- Provision (model switch March 2026):
--   bs_prov = prmt.new_bs_prov     for transaction_month >= 2026-03-01
--           = prmt.current_bs_provision otherwise
--   This isolates the model artefact noted in handover Section 6 — never use
--   ptn.is_charge directly for charge attribution.
--
-- Collections split (from fttc):
--   instalment_collections — txn_category = 'Instalment Receipt' (DebiCheck)
--   effort_collections     — txn_category = 'Other Receipt' (agent / arrangement)
--   is_active_payer        — any negative receipt posted within the month
--
-- MPM bands:
--   Closing  mpm_band      from mds.months_past_maturity
--   Opening  prev_mpm_band from mds.prev_months_past_maturity
--   Stratify the OOT book by depth — recovery economics change sharply
--   between 0–3, 4–6, 7–12, 13–24 and 24+ months past maturity.
--
-- Activation & lapse:
--   first_oot          = first month a loan enters OOT (bucket = 'Out of Term')
--   first_oot_receipt  = first receipt after first_oot_month → activation_months
--   ever_paid          = ever received any receipt since first_oot_month
--   active_payer       = received a receipt in the current transaction_month
--   lapsed_payer       = ever paid, but not in this month
--   never_paid         = never paid since OOT entry
-- ============================================================

WITH params AS (SELECT DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month'   AS reporting_month,
                       DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '13 months' AS window_start),

-- ── Narrow ptn CTE ────────────────────────────────────────────
-- opening_balance, closing_balance, provision fields not in mds
     ptn_flows AS (SELECT ptn.loannumber,
                          ptn.transaction_month::date AS transaction_month,
                          ptn.opening_balance,
                          ptn.closing_balance,
                          ptn.statement_number,
                          ptn.term,
--                           mwat.is_written_off,
                          -- Provision balance — model switch March 2026
                          CASE
                              WHEN ptn.transaction_month::date >= DATE '2026-03-01'
                                  THEN prmt.new_bs_prov
                              ELSE prmt.current_bs_provision
                              END                     AS bs_prov
                   FROM fasta_views.payments_table_full ptn
                            LEFT JOIN fasta_views.provision_review_master_table prmt
                                      ON prmt.loannumber = ptn.loannumber
                                          AND prmt.transaction_month = ptn.transaction_month
                       --                             JOIN (SELECT loannumber, report_month as transaction_month, is_written_off
--                                   FROM fasta_views.mv_writeoff_audit_trail) mwat
--                                  ON ptn.loannumber = mwat.loannumber AND ptn.transaction_month = mwat.transaction_month
--                                  --AND mwat.is_written_off=FALSE
                            JOIN fasta_views.mv_commercial_opening_activity act
                                 ON ptn.loannumber = act.loannumber AND ptn.transaction_month = act.transaction_month
                                     AND act.opening_is_active IN (1, 2, 3, 9999)
                                     AND act.opening_in_term_flag = FALSE
                            JOIN params p
                                 ON ptn.transaction_month::date BETWEEN p.window_start AND p.reporting_month),

-- ── Collections split from FTTC ──────────────────────────────
-- Total, instalment (debit order) and effort per loan per month
-- Active payer: any receipt posted within transaction_month cycle
     fttc_collections AS (SELECT fttc.loannumber,
                                 DATE_TRUNC('month', fttc.txn_date)::date AS transaction_month,

                                 SUM(fttc.txn_amount) FILTER (
                                     WHERE fttc.txn_category = 'Instalment Receipt'
                                     ) * -1                               AS instalment_collections,

                                 SUM(fttc.txn_amount) FILTER (
                                     WHERE fttc.txn_category = 'Other Receipt'
                                     ) * -1                               AS effort_collections,

                                 -- Active payer: any receipt in this transaction_month cycle
                                 MAX(1) FILTER (
                                     WHERE fttc.txn_amount < 0
                                     )                                    AS is_active_payer

                          FROM fasta_views.financialtransactions_transactions_corrected fttc
                                   JOIN params p
                                        ON DATE_TRUNC('month', fttc.txn_date)::date
                                            BETWEEN p.window_start AND p.reporting_month
                          WHERE fttc.txn_group = 'Net Receipts'
                            AND fttc.txn_category IN ('Instalment Receipt', 'Other Receipt')
                          GROUP BY 1, 2),

-- ── First OOT month per loan ──────────────────────────────────
-- Anchor for activation rate and recovery lag
-- First month where delinquency_bucket = 'Out of Term' in MDS
     first_oot AS (SELECT mds.loannumber,
                          MIN(mds.transaction_month::date) AS first_oot_month
                   FROM fasta_views.mv_delinquency_segments mds
                   WHERE mds.delinquency_bucket = 'Out of Term'
                   GROUP BY 1),

-- ── First receipt date per loan ───────────────────────────────
-- Months from first_oot_month to first payment received after OOT entry
-- NULL if never paid — activation_months = 0 means paid in same month
     first_oot_receipt AS (SELECT fo.loannumber,
                                  fo.first_oot_month,
                                  MIN(fttc.txn_date)::date AS first_receipt_date,
                                  CASE
                                      WHEN MIN(fttc.txn_date) IS NOT NULL
                                          THEN (
                                                   DATE_PART('year', MIN(fttc.txn_date)::date) * 12
                                                       + DATE_PART('month', MIN(fttc.txn_date)::date)
                                                   ) - (
                                                   DATE_PART('year', fo.first_oot_month) * 12
                                                       + DATE_PART('month', fo.first_oot_month)
                                                   )
                                      ELSE NULL
                                      END                  AS activation_months
                           FROM first_oot fo
                                    LEFT JOIN fasta_views.financialtransactions_transactions_corrected fttc
                                              ON fttc.loannumber = fo.loannumber
                                                  AND fttc.txn_date >= fo.first_oot_month
                                                  AND fttc.txn_amount < 0
                                                  AND fttc.txn_group = 'Net Receipts'
                                                  AND fttc.txn_category IN ('Instalment Receipt', 'Other Receipt')
                           GROUP BY 1, 2),

-- ── Ever paid since OOT ───────────────────────────────────────
-- Drives lapsed vs never-paid classification
     ever_paid AS (SELECT DISTINCT fttc.loannumber
                   FROM fasta_views.financialtransactions_transactions_corrected fttc
                            JOIN first_oot fo
                                 ON fo.loannumber = fttc.loannumber
                                     AND fttc.txn_date >= fo.first_oot_month
                   WHERE fttc.txn_amount < 0
                     AND fttc.txn_group = 'Net Receipts'
                     AND fttc.txn_category IN ('Instalment Receipt', 'Other Receipt'))

-- ── Final aggregation ─────────────────────────────────────────
SELECT mds.transaction_month::date                               AS transaction_month,
       pt.product,

       -- Opening MPM band — SOM position
       CASE
           WHEN mds.prev_months_past_maturity BETWEEN 0 AND 3 THEN '01 — 0–3 months'
           WHEN mds.prev_months_past_maturity BETWEEN 4 AND 6 THEN '02 — 4–6 months'
           WHEN mds.prev_months_past_maturity BETWEEN 7 AND 12 THEN '03 — 7–12 months'
           WHEN mds.prev_months_past_maturity BETWEEN 13 AND 24 THEN '04 — 13–24 months'
           WHEN mds.prev_months_past_maturity > 24 THEN '05 — 24+ months'
           ELSE '00 — Unknown'
           END                                                   AS prev_mpm_band,

       -- Closing MPM band — EOM position
       CASE
           WHEN mds.months_past_maturity BETWEEN 0 AND 3 THEN '01 — 0–3 months'
           WHEN mds.months_past_maturity BETWEEN 4 AND 6 THEN '02 — 4–6 months'
           WHEN mds.months_past_maturity BETWEEN 7 AND 12 THEN '03 — 7–12 months'
           WHEN mds.months_past_maturity BETWEEN 13 AND 24 THEN '04 — 13–24 months'
           WHEN mds.months_past_maturity > 24 THEN '05 — 24+ months'
           ELSE '00 — Unknown'
           END                                                   AS mpm_band,

       -- ── Portfolio ─────────────────────────────────────────────
       COUNT(DISTINCT mds.loannumber)                            AS loan_count,
       ROUND(SUM(f.opening_balance), 2)                          AS opening_balance,
       ROUND(SUM(f.closing_balance), 2)                          AS closing_balance,
       ROUND(SUM(f.closing_balance) - SUM(f.opening_balance), 2) AS balance_movement,

       -- ── Collections ───────────────────────────────────────────
       ROUND(SUM(mds.net_receipts), 2)                           AS total_collections,
       ROUND(COALESCE(SUM(fc.instalment_collections), 0), 2)     AS instalment_collections,
       ROUND(COALESCE(SUM(fc.effort_collections), 0), 2)         AS effort_collections,

       -- Yield: total collections ÷ opening_balance
       ROUND(
               SUM(mds.net_receipts)
                   / NULLIF(SUM(f.opening_balance), 0) * 100
           , 2)                                                  AS yield_pct,

       -- Payment mix
       ROUND(
               COALESCE(SUM(fc.instalment_collections), 0)
                   / NULLIF(SUM(mds.net_receipts), 0) * 100
           , 2)                                                  AS instalment_pct_of_collections,
       ROUND(
               COALESCE(SUM(fc.effort_collections), 0)
                   / NULLIF(SUM(mds.net_receipts), 0) * 100
           , 2)                                                  AS effort_pct_of_collections,

       -- ── Payer segmentation ────────────────────────────────────
       COUNT(DISTINCT mds.loannumber)
       FILTER (WHERE fc.is_active_payer = 1)                     AS active_payers,
       COUNT(DISTINCT mds.loannumber)
       FILTER (WHERE fc.is_active_payer IS NULL
           AND ep.loannumber IS NOT NULL)                        AS lapsed_payers,
       COUNT(DISTINCT mds.loannumber)
       FILTER (WHERE ep.loannumber IS NULL)                      AS never_paid,
       ROUND(
               100.0 * COUNT(DISTINCT mds.loannumber)
                       FILTER (WHERE fc.is_active_payer = 1)
                   / NULLIF(COUNT(DISTINCT mds.loannumber), 0)
           , 2)                                                  AS active_payer_rate_pct,
       ROUND(
               SUM(mds.net_receipts)
                   / NULLIF(COUNT(DISTINCT mds.loannumber)
                            FILTER (WHERE fc.is_active_payer = 1), 0)
           , 2)                                                  AS avg_payment_per_active_payer,

       -- ── Activation rate ───────────────────────────────────────
       AVG(fr.activation_months)                                 AS avg_activation_months,
       AVG(fr.activation_months)
       FILTER (WHERE fr.activation_months IS NOT NULL)           AS avg_activation_months_payers_only,
       MIN(fr.activation_months)                                 AS min_activation_months,
       MAX(fr.activation_months)                                 AS max_activation_months,
       ROUND(
               100.0 * COUNT(DISTINCT mds.loannumber)
                       FILTER (WHERE ep.loannumber IS NOT NULL)
                   / NULLIF(COUNT(DISTINCT mds.loannumber), 0)
           , 2)                                                  AS activation_rate_pct,

       -- ── Recovery lag ─────────────────────────────────────────
       AVG(
               (DATE_PART('year', mds.transaction_month::date) * 12
                   + DATE_PART('month', mds.transaction_month::date))
                   - (DATE_PART('year', fo.first_oot_month) * 12
                   + DATE_PART('month', fo.first_oot_month))
       )                                                         AS avg_months_in_oot,
       COUNT(DISTINCT mds.loannumber) FILTER (WHERE (
                                                        (DATE_PART('year', mds.transaction_month::date) * 12
                                                            + DATE_PART('month', mds.transaction_month::date))
                                                            - (DATE_PART('year', fo.first_oot_month) * 12
                                                            + DATE_PART('month', fo.first_oot_month))
                                                        ) <= 3)  AS loans_oot_0_3_months,
       COUNT(DISTINCT mds.loannumber) FILTER (WHERE (
           (DATE_PART('year', mds.transaction_month::date) * 12
               + DATE_PART('month', mds.transaction_month::date))
               - (DATE_PART('year', fo.first_oot_month) * 12
               + DATE_PART('month', fo.first_oot_month))
           ) BETWEEN 4 AND 12)                                   AS loans_oot_4_12_months,
       COUNT(DISTINCT mds.loannumber) FILTER (WHERE (
                                                        (DATE_PART('year', mds.transaction_month::date) * 12
                                                            + DATE_PART('month', mds.transaction_month::date))
                                                            - (DATE_PART('year', fo.first_oot_month) * 12
                                                            + DATE_PART('month', fo.first_oot_month))
                                                        ) > 12)  AS loans_oot_12plus_months,

       -- ── Provision ────────────────────────────────────────────
       ROUND(SUM(f.bs_prov), 2)                                  AS provision_balance,
       ROUND(SUM(f.closing_balance) - SUM(f.bs_prov), 2)         AS provision_coverage_gap,
       ROUND(
               SUM(f.bs_prov)
                   / NULLIF(SUM(f.closing_balance), 0) * 100
           , 2)                                                  AS provision_coverage_pct

FROM fasta_views.mv_delinquency_segments mds

-- Product classification
         JOIN fasta_views.mv_commercial_loan_product_type pt
              ON mds.loannumber = pt.loannumber

-- Narrow ptn fields + provision
         JOIN ptn_flows f
              ON mds.loannumber = f.loannumber
                  AND mds.transaction_month::date = f.transaction_month

-- Collections split
         LEFT JOIN fttc_collections fc
                   ON mds.loannumber = fc.loannumber
                       AND mds.transaction_month::date = fc.transaction_month

-- First OOT month anchor
         LEFT JOIN first_oot fo
                   ON fo.loannumber = mds.loannumber

-- Activation rate
         LEFT JOIN first_oot_receipt fr
                   ON fr.loannumber = mds.loannumber

-- Ever paid since OOT
         LEFT JOIN ever_paid ep
                   ON ep.loannumber = mds.loannumber


WHERE mds.transaction_month::date BETWEEN
              (SELECT window_start FROM params)
          AND (SELECT reporting_month FROM params)


GROUP BY 1, 2, 3, 4
ORDER BY 1 DESC, 2, 3;
