-- ════════════════════════════════════════════════════════════
-- Q08b — Internal Collections IT: Operational case
-- ════════════════════════════════════════════════════════════

WITH params AS (
    SELECT
        DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month'   AS reporting_month,
        DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '13 months'  AS window_start
),

-- Narrow ptn CTE — only fields not available in mds
ptn_flows AS (
    SELECT
        ptn.loannumber,
        ptn.transaction_month::date                             AS transaction_month,
        ptn.instalment_due_in_month,
        ptn.opening_balance                                     AS opening_balance
    FROM fasta_views.payments_table_full ptn
    JOIN params p
        ON ptn.transaction_month::date BETWEEN p.window_start AND p.reporting_month
),

-- Collections split from fttc — effort vs instalment per loan per month
fttc_collections AS (
    SELECT
        fttc.loannumber,
        DATE_TRUNC('month', fttc.txn_date)::date               AS transaction_month,

        SUM(fttc.txn_amount) FILTER (
            WHERE fttc.txn_category = 'Instalment Receipt'
        ) * -1                                                  AS instalment_collections,

        SUM(fttc.txn_amount) FILTER (
            WHERE fttc.txn_category = 'Other Receipt'
        ) * -1                                                  AS effort_collections,

        -- Effort payer flag — did an agent-sourced payment come in this month?
        COUNT(DISTINCT fttc.loannumber) FILTER (
            WHERE fttc.txn_category = 'Other Receipt'
              AND fttc.txn_amount < 0
        )                                                       AS is_effort_payer

    FROM fasta_views.financialtransactions_transactions_corrected fttc
    JOIN params p
        ON DATE_TRUNC('month', fttc.txn_date)::date
           BETWEEN p.window_start AND p.reporting_month
    WHERE fttc.txn_group = 'Net Receipts'
      AND fttc.txn_category IN ('Instalment Receipt', 'Other Receipt')
    GROUP BY 1, 2
),

-- Effective arrears — true obligation under management per loan per month
-- Derived at loan level before aggregation
-- Requires: mds (opening/closing arrears, prev_segment), fttc (auto collected flag)
loan_level AS (
    SELECT
        mds.loannumber,
        mds.transaction_month::date                             AS transaction_month,
        mds.prev_delinquency_segment                            AS mp_opening,
        mds.delinquency_segment                                 AS mp_closing,
        mds.prev_delinquency_bucket                             AS bucket_opening,
        mds.delinquency_bucket                                  AS bucket_closing,
        mds.opening_arrears,
        mds.closing_arrears,
        mds.net_receipts                                        AS total_collected,
        COALESCE(f.instalment_collections, 0)                   AS instalment_collections,
        COALESCE(f.effort_collections, 0)                       AS effort_collections,
        COALESCE(f.is_effort_payer, 0)                          AS is_effort_payer,
        COALESCE(ptn.instalment_due_in_month, 0)                AS instalment_due,

        -- Effective arrears — true obligation under management
        CASE
            WHEN COALESCE(mds.prev_delinquency_segment, 'MP0') = 'MP0'
                -- New entrant: no prior arrears, missed instalment is full obligation
                THEN COALESCE(ptn.instalment_due_in_month, 0)

            WHEN COALESCE(f.instalment_collections, 0) > 0
                -- DebiCheck ran: residual closing arrears is true position
                THEN GREATEST(mds.closing_arrears, 0)

            ELSE
                -- No auto collection: full past due plus current instalment
                mds.opening_arrears + COALESCE(ptn.instalment_due_in_month, 0)
        END                                                     AS effective_arrears,

        -- MP rank for cure/deterioration direction
        CASE COALESCE(mds.prev_delinquency_segment, 'MP0')
            WHEN 'MP0'  THEN 0
            WHEN 'MP1'  THEN 1
            WHEN 'MP2'  THEN 2
            WHEN 'MP3+' THEN 3
            ELSE 0
        END                                                     AS mp_opening_rank,

        CASE mds.delinquency_segment
            WHEN 'MP0'  THEN 0
            WHEN 'MP1'  THEN 1
            WHEN 'MP2'  THEN 2
            WHEN 'MP3+' THEN 3
            ELSE 0
        END                                                     AS mp_closing_rank

    FROM fasta_views.mv_delinquency_segments mds
    -- Department at start of month
    JOIN fasta_views.mv_department_start_of_month mdsm
        ON mds.loannumber = mdsm.loannumber
            AND mds.transaction_month = mdsm.reporting_month
    -- Active-at-SOM gate
    JOIN fasta_views.mv_commercial_opening_activity act
        ON  mds.loannumber        = act.loannumber
        AND mds.transaction_month::date = act.transaction_month
        AND act.opening_is_active IN (1, 2, 3, 9999)

    -- Narrow ptn fields
    LEFT JOIN ptn_flows ptn
        ON  mds.loannumber        = ptn.loannumber
        AND mds.transaction_month::date = ptn.transaction_month

    -- Collections split
    LEFT JOIN fttc_collections f
        ON  mds.loannumber        = f.loannumber
        AND mds.transaction_month::date = f.transaction_month

    WHERE mds.transaction_month::date BETWEEN
              (SELECT window_start FROM params)
          AND (SELECT reporting_month FROM params)
      -- In-term only — this is the in-term operational view
      AND act.opening_in_term_flag = TRUE
    AND (mdsm.department_start_of_month in ('Internal Collections - in term') or
                              mdsm.touched_it_in_month)
)

SELECT
    ll.transaction_month,

    -- Total effort collected across all in-term segments
    ROUND(SUM(ll.effort_collections), 2)                        AS total_effort_collected,

    -- Internal cost per rand — team cost ÷ total effort collected
    -- Green if < 0.15 (cheaper than 1st placement)
    -- Amber if 0.15–0.20 (between placement rates)
    -- Red if > 0.20 (more expensive than 2nd placement)
    ROUND(
        198000.0
        / NULLIF(SUM(ll.effort_collections), 0), 4
    )                                                           AS internal_cost_per_rand,

    -- What outsourcing would have cost at each placement rate
    ROUND(SUM(ll.effort_collections) * 0.15, 2)                AS placement_1st_equivalent_cost,
    ROUND(SUM(ll.effort_collections) * 0.20, 2)                AS placement_2nd_equivalent_cost,

    -- Monthly saving vs outsourcing (positive = team is cheaper)
    -- Break-even effort collected = R198,000 ÷ 0.15 = R1,320,000
    ROUND(SUM(ll.effort_collections) * 0.15 - 198000.0, 2)     AS monthly_saving_vs_1st_placement,
    ROUND(SUM(ll.effort_collections) * 0.20 - 198000.0, 2)     AS monthly_saving_vs_2nd_placement,

    -- Break-even reference — effort needed to justify team cost at 1st placement rate
    1320000.00                                                  AS breakeven_effort_collected

FROM loan_level ll

-- WHERE COALESCE(ll.mp_opening, 'MP0') NOT LIKE 'MPM%'

GROUP BY 1
ORDER BY 1 DESC
