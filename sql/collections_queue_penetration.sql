-- penetration_rate & penetration_rate_adj: the literal percentage of out collections queue
-- that actually has an arranged payment promise (ptp) associated with it.

-- ptp_fulfillment_rate: the percentage of those created arrangements that actually settled successfully (kept rate).
-- total_queue_exposure: the combined original installment financial liability rolling into collections for that reporting segment.
-- recovered_volume: the actual monetary volume brought back to a successful state via kept arrangements.


with params AS (select date_trunc('month', current_date) - interval '1 month' as reporting_month),
     payments_base as (select l.loannumber,
                              l.id                                                             as loanid,
                              l.currentloanstate_loanstatetype,
                              l.created::date,
                              l.loanperiod_numberofperiods,
                              z ->> '_id'                                                      as id,
                              z ->> 'paymentType'                                              as payment_type,
                              z -> 'updatedBy' ->> 'username'                                  as updated_by,
                              make_date(
                                      (z -> 'plannedPayment' -> 'date' ->> 'year')::int,
                                      LEAST((z -> 'plannedPayment' -> 'date' ->> 'month')::int + 1, 12),
                                      (z -> 'plannedPayment' -> 'date' ->> 'day')::int
                              )                                                                as planned_date,
                              (z -> 'plannedPayment' -> 'amount' ->> 'amount')::numeric(12, 2) as planned_amount,
                              z -> 'plannedPayment' -> 'receiptRequests' -> 0 ->> 'timestamp'  as receipt_request_timestamp,
                              ar._id,
                              ar.timestamp,
                              ar."actionDate",
                              ar.status
-- jsonb_to_record_set for requestStatuses
                       from loans l
                                join loan_paymentarrangements lp
                                     on lp.loanid = l.id and l.created > make_date(2022, 01, 01),
                            unnest(lp.payments) z,
                            jsonb_to_recordset(z -> 'plannedPayment' -> 'receiptRequests' -> 0 -> 'requestStatuses') AS ar("_id" text,
                                                                                                                           "data" jsonb,
                                                                                                                           "status" text,
                                                                                                                           "requestId" text,
                                                                                                                           "timestamp" text,
                                                                                                                           "actionDate" text)
                       where 1 = 1
                         and fromdate > make_date(2018, 01, 01)
                         and z ->> 'paymentType' in ('PTP', 'Other')
     )
        ,

     ranked_transactions as (select loannumber,
                                    payment_type,
                                    id                                                                      as payment_id,
                                    coalesce(planned_date, timestamp::date)                                 as scheduled_date,
                                    planned_amount,
                                    timestamp                                                               as transaction_dt,
                                    status,
                                    dense_rank() over (partition by loannumber, id order by timestamp desc) as transaction_order
                             from payments_base),

     -- One row per (loannumber, scheduled_month): the attempted PTP debit orders.
     -- Pre-aggregated to the same grain as settled_ptp_orders so the master joins
     -- stay 1:1 (joining the ungrouped per-DO rows fanned out the settled sums).
     ptp_orders as (select loannumber,
                           date_trunc('month', scheduled_date)::date as scheduled_month,
                           sum(planned_amount)                       as ptp_do_amount,
                           count(*)                                  as number_of_ptp_do
                    from ranked_transactions
                    where transaction_order = 1
                    group by 1, 2),

     settled_ptp_orders as (select loannumber,
                                   date_trunc('month', scheduled_date)::date as scheduled_month,
                                   sum(planned_amount)                       as ptp_do_amount,
                                   count(*)                                  as number_of_settled_ptp_do
                            from ranked_transactions
                            where transaction_order = 1
                              and status in ('successful')
                            group by 1, 2),

     unsettled_ptp_orders as (select loannumber,
                                     date_trunc('month', scheduled_date)::date as scheduled_month,
                                     sum(planned_amount)                       as ptp_do_amount,
                                     count(*)                                  as number_of_unsettled_ptp_do
                              from ranked_transactions
                              where transaction_order = 1
                                and status not in ('successful')
                              group by 1, 2)
        ,


     active_base as (select mdsom.loannumber,
                            mdsom.reporting_month,
                            mdsom.department_start_of_month,
                            coalesce(mdsom.dept_at_end, mdsom.department_start_of_month) as department_end_of_month,
                            mdsom.department,
                            mdsom.department_end,
                            mdsom.instalment_due_in_month,
                            round((mdsom.reporting_month - first_value(mdsom.reporting_month) over
                                (partition by mdsom.loannumber order by mdsom.reporting_month)) / 30.42)
                                                                                         as months_in_it
                             ,
                            max(mdsom.instalment_due_in_month)
                            over (partition by mdsom.loannumber)                         as original_instalment_amount,
                            mdsom.opening_balance,
                            mdsom.closing_balance,
                            mdsom.net_receipts,
                            mdsom.arrears
                     from fasta_views.mv_department_start_of_month mdsom
                     where (mdsom.department_start_of_month in ('Internal Collections - in term') or
                            mdsom.touched_it_in_month)

     )
        ,
     -- Add attempted and successful ptp debit order volume to active base
     master as (select ab.loannumber,
                       ab.reporting_month,
                       ab.months_in_it,
                       ab.instalment_due_in_month,
                       ab.original_instalment_amount,
                       ab.department_start_of_month,
                       ab.department_end_of_month,
                       ab.opening_balance,
                       ab.closing_balance,
                       ab.net_receipts,
                       mds.opening_arrears as arrears,
                       coalesce(sum(ptpo.ptp_do_amount), 0)              as attempted_recovery_volume_in_month,
                       coalesce(sum(ptpoi.ptp_do_amount), 0)             as attempted_recovery_volume_in_next_month,
                       coalesce(sum(ptpo.ptp_do_amount), 0) +
                       coalesce(sum(ptpoi.ptp_do_amount), 0)             as attempted_recovery_volume,
                       coalesce(sum(ptpo.number_of_ptp_do), 0)           as number_of_attempted_ptp_dos_in_month,
                       coalesce(sum(ptpoi.number_of_ptp_do), 0)          as number_of_attempted_ptp_dos_in_next_month,
                       coalesce(sum(ptpo.number_of_ptp_do), 0) +
                       coalesce(sum(ptpoi.number_of_ptp_do), 0)          as number_of_attempted_ptp_dos,
                       coalesce(sum(ptpos.number_of_settled_ptp_do), 0)  as number_of_settled_ptp_do_in_month,
                       coalesce(sum(ptposi.number_of_settled_ptp_do), 0) as number_of_settled_ptp_do_in_next_month,
                       coalesce(sum(ptpos.number_of_settled_ptp_do), 0) +
                       coalesce(sum(ptposi.number_of_settled_ptp_do), 0) as number_of_settled_ptp_do,
                       coalesce(sum(ptpos.ptp_do_amount), 0)             as amount_recovered_in_month,
                       coalesce(sum(ptposi.ptp_do_amount), 0)            as amount_recovered_in_next_month,
                       coalesce(sum(ptpos.ptp_do_amount), 0) +
                       coalesce(sum(ptposi.ptp_do_amount), 0)            as amount_recovered
                from active_base ab
                    left join fasta_views.mv_delinquency_segments mds on ab.loannumber = mds.loannumber
                                                         and ab.reporting_month = mds.transaction_month
                         left join ptp_orders ptpo on ptpo.loannumber = ab.loannumber and
                                                      (ab.reporting_month = ptpo.scheduled_month)
                         left join ptp_orders ptpoi on ptpoi.loannumber = ab.loannumber and
                                                       (ab.reporting_month + interval '1 month' = ptpoi.scheduled_month)
                         left join settled_ptp_orders ptpos on ptpos.loannumber = ab.loannumber and
                                                               (ab.reporting_month = ptpos.scheduled_month)
                         left join settled_ptp_orders ptposi on ptposi.loannumber = ab.loannumber and
                                                                (ab.reporting_month + interval '1 month' =
                                                                 ptposi.scheduled_month)
                group by 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11)
        ,

     final_summary as (select loannumber,
                              reporting_month,
                              months_in_it,
                              department_start_of_month,
                              department_end_of_month,
                              original_instalment_amount,
                              instalment_due_in_month,
                              opening_balance,
                              closing_balance,
                              net_receipts,
                              arrears,
                              attempted_recovery_volume_in_month,
                              attempted_recovery_volume_in_next_month,
                              amount_recovered_in_month,
                              amount_recovered_in_next_month,
                              amount_recovered,
                              attempted_recovery_volume,
                              number_of_settled_ptp_do,
                              number_of_settled_ptp_do_in_month,
                              number_of_settled_ptp_do_in_next_month,
                              number_of_attempted_ptp_dos,
                              number_of_attempted_ptp_dos_in_month,
                              number_of_attempted_ptp_dos_in_next_month,
                              case when number_of_attempted_ptp_dos_in_month > 0 then 1 else 0 end      as has_ptp_dos_in_month,
                              case when number_of_attempted_ptp_dos_in_next_month > 0 then 1 else 0 end as has_ptp_dos_in_next_month,
                              case when number_of_attempted_ptp_dos > 0 then 1 else 0 end               as has_ptp_dos
                       from master)

select reporting_month,
       department_start_of_month,
       department_end_of_month,

       -- opening and closing balance and net_receipts
       sum(opening_balance)                          as opening_balance,
       sum(closing_balance)                          as closing_balance,
       sum(instalment_due_in_month)                  as instalment_due_in_month,
       sum(arrears)                                  as arrears,
       (sum(arrears) + sum(instalment_due_in_month)) as total_due_in_month,
       sum(net_receipts)                             as net_receipts,
       -- collection yields
       round((sum(net_receipts)::numeric / nullif((sum(arrears)::numeric + sum(instalment_due_in_month)::numeric)::numeric, 0)) * 100,
             2)                                      as collections_yield_pct,

       -- penetration volumes
       count(*)                                      as number_of_loans,
       sum(has_ptp_dos_in_month)                     as number_of_loans_with_ptp_dos_in_month,
       sum(has_ptp_dos_in_next_month)                as number_of_loans_with_ptp_dos_in_next_month,
       sum(has_ptp_dos)                              as number_of_loans_with_ptp_dos,

       -- ptp arrangement counts (kept/made) — exposed so penetration & fulfillment
       -- can be re-derived from count sums when aggregating across end-departments
       sum(number_of_attempted_ptp_dos_in_month)      as number_of_attempted_ptp_dos_in_month,
       sum(number_of_attempted_ptp_dos_in_next_month) as number_of_attempted_ptp_dos_in_next_month,
       sum(number_of_attempted_ptp_dos)               as number_of_attempted_ptp_dos,
       sum(number_of_settled_ptp_do_in_month)         as number_of_settled_ptp_do_in_month,
       sum(number_of_settled_ptp_do_in_next_month)    as number_of_settled_ptp_do_in_next_month,
       sum(number_of_settled_ptp_do)                  as number_of_settled_ptp_do,

       -- volume exposures
       sum(original_instalment_amount)               as total_queue_exposure,
       sum(attempted_recovery_volume_in_month)                as attempted_recovery_volume_in_month,
       sum(attempted_recovery_volume_in_next_month)                as attempted_recovery_volume_in_next_month,
       sum(attempted_recovery_volume)                as attempted_recovered_volume,
       sum(amount_recovered_in_month)                         as total_recovered_volume_in_month,
       sum(amount_recovered_in_next_month)                         as total_recovered_volume_in_next_month,
       sum(amount_recovered)                         as total_recovered_volume,

       -- recovery yields
       round((sum(amount_recovered)::numeric / nullif(sum(original_instalment_amount)::numeric, 0)) * 100,
             2)                                      as recovery_yield_pct,

       round((sum(amount_recovered_in_month)::numeric / nullif(sum(original_instalment_amount)::numeric, 0)) *
             100,
             2)                                      as recovery_yield_in_month_pct,

       round((sum(amount_recovered_in_next_month)::numeric /
              nullif(sum(original_instalment_amount)::numeric, 0)) * 100,
             2)                                      as recovery_yield_next_month_pct,

       -- penetration percentages (conversion tracking)
       round((sum(has_ptp_dos_in_month)::numeric / nullif(count(*), 0)) * 100,
             2)                                      as penetration_rate_in_month_pct,
       round((sum(has_ptp_dos_in_next_month)::numeric / nullif(count(*), 0)) * 100,
             2)                                      as penetration_rate_in_next_month_pct,
       round((sum(has_ptp_dos)::numeric / nullif(count(*), 0)) * 100,
             2)                                      as penetration_rate_pct,


       -- arrangement fulfillment performance
       round((sum(number_of_settled_ptp_do_in_month)::numeric / nullif(sum(number_of_attempted_ptp_dos_in_month), 0)) *
             100,
             2)                                      as ptp_fulfillment_rate_in_month_pct,
       round((sum(number_of_settled_ptp_do_in_next_month)::numeric /
              nullif(sum(number_of_attempted_ptp_dos_in_next_month), 0)) * 100,
             2)                                      as ptp_fulfillment_rate_in_next_month_pct,
       round((sum(number_of_settled_ptp_do)::numeric / nullif(sum(number_of_attempted_ptp_dos), 0)) * 100,
             2)                                      as ptp_fulfillment_rate_pct
from final_summary
where reporting_month::date between
              (select reporting_month from params) - interval '13 months'
          and (select reporting_month from params)
group by 1, 2, 3
order by 1 desc, 4 desc;

