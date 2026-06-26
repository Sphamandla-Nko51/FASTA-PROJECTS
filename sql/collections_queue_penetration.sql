-- penetration_rate & penetration_rate_adj: the literal percentage of out collections queue
-- that actually has an arranged payment promise (ptp) associated with it.

-- ptp_fulfillment_rate: the percentage of those created arrangements that actually settled successfully (kept rate).
-- total_queue_exposure: the combined original installment financial liability rolling into collections for that reporting segment.
-- recovered_volume: the actual monetary volume brought back to a successful state via kept arrangements.



with params AS (select date_trunc('month', current_date) - interval '1 month' as reporting_month),
payments_base as (
    select
    l.loannumber,
    l.id as loanid,
    l.currentloanstate_loanstatetype,
    l.created::date,
    l.loanperiod_numberofperiods,
    z ->> '_id' as id,
    z ->> 'paymentType' as payment_type,
    z ->'updatedBy' ->> 'username' as updated_by,
    make_date(
      (z -> 'plannedPayment'->'date'->>'year')::int,
      LEAST((z -> 'plannedPayment'->'date'->>'month')::int+1,12),
      (z -> 'plannedPayment'->'date'->>'day')::int
      ) as planned_date,
    (z->'plannedPayment'->'amount'->>'amount')::numeric(12,2) as planned_amount,
    z -> 'plannedPayment' -> 'receiptRequests' -> 0 ->> 'timestamp' as receipt_request_timestamp,
    ar._id,
    ar.timestamp,
    ar."actionDate",
    ar.status
-- jsonb_to_record_set for requestStatuses
from loans l
join loan_paymentarrangements lp on lp.loanid = l.id and l.created > make_date(2022,01,01),
    unnest(lp.payments) z,
    jsonb_to_recordset(z -> 'plannedPayment' -> 'receiptRequests' -> 0 -> 'requestStatuses')  AS ar("_id" text, "data" jsonb, "status" text, "requestId" text, "timestamp" text, "actionDate" text)
where 1=1
and fromdate > make_date(2018,01,01)
and z ->> 'paymentType' in  ('PTP')
-- and loannumber = '0005669373'
-- and loannumber = '0001283180'
-- limit 10;;
)
,

ranked_transactions as (

select loannumber,
       payment_type,
       id as payment_id,
       planned_date as scheduled_date,
       planned_amount,
       timestamp as transaction_dt,
       status,
       dense_rank() over (partition by loannumber, id order by timestamp desc) as transaction_order
from payments_base
),

ptp_orders as (
    select loannumber, date_trunc('month', scheduled_date)::date as scheduled_month, planned_amount as ptp_do_amount, 1 as has_ptp_do,
       case when status in ('successful') then 1 else 0 end as is_kept
       from ranked_transactions where transaction_order = 1
),

active_base as (
    select mdsom.loannumber,
       mdsom.reporting_month,
       mdsom.department_start_of_month,
       coalesce(mdsom.dept_at_end,mdsom.department_start_of_month) as department_end_of_month,
       mdsom.department,
       mdsom.department_end,
       mdsom.instalment_due_in_month,
               round((mdsom.reporting_month - first_value (mdsom.reporting_month) over
           (partition by mdsom.loannumber order by mdsom.reporting_month))/30.42)
           as months_in_it
       ,
        max(mdsom.instalment_due_in_month) over (partition by mdsom.loannumber) as original_instalment_amount
from fasta_views.mv_department_start_of_month mdsom
where

--     and  mdsom.reporting_month::date between
--         (select reporting_month from params) - interval '13 months'
--     and (select reporting_month from params)
 (mdsom.department_start_of_month in ('Internal Collections - in term') or
                              mdsom.touched_it_in_month)
-- and  mdsom.loannumber = '0005669373'
-- and  mdsom.loannumber = '0001283180'

    )
     ,

    master as (
    select
        ab.loannumber,
        ab.reporting_month,
        ab.months_in_it,
        ab.instalment_due_in_month,
        ab.original_instalment_amount,
        ab.department_start_of_month,
        ab.department_end_of_month,
        sum(ptpo.has_ptp_do) as number_of_ptp_dos,
        sum(case when ptpo.ptp_do_amount / nullif(ab.original_instalment_amount, 0) > 0.8 and ptpo.has_ptp_do > 0 then 1 else 0 end) as number_of_ptp_dos_adj,
        sum(ptpo.is_kept) as number_of_kept_dos,
        sum(case when ptpo.ptp_do_amount / nullif(ab.original_instalment_amount, 0) > 0.8 and ptpo.is_kept > 0 then 1 else 0 end) as number_of_kept_dos_adj,
        sum(case when ptpo.is_kept = 1 then ptpo.ptp_do_amount else 0 end) as amount_recovered,
        sum(case when ptpo.ptp_do_amount / nullif(ab.original_instalment_amount, 0) > 0.8 and ptpo.is_kept > 0  then ptpo.ptp_do_amount else 0 end) as amount_recovered_adj
from active_base ab
join ptp_orders ptpo on ptpo.loannumber = ab.loannumber and
                            ab.reporting_month >= scheduled_month
group by 1, 2, 3, 4, 5, 6, 7
    ),

final_summary as (
select
        loannumber, reporting_month, months_in_it, department_start_of_month, department_end_of_month,
        original_instalment_amount, amount_recovered, number_of_kept_dos, number_of_ptp_dos,, number_of_kept_dos_adj, number_of_ptp_dos_adj,
        case when number_of_ptp_dos > 0 and number_of_ptp_dos >= months_in_it then 1 else 0 end as has_ptp_dos_in_month,
        case when number_of_ptp_dos_adj > 0 then 1 else 0 end as has_ptp_dos_in_month_adj
    from master
)

select
    reporting_month,
    department_start_of_month,
    department_end_of_month,
    count(*) as number_of_loans,

    -- volume exposures
    sum(original_instalment_amount) as total_queue_exposure,
    sum(amount_recovered) as total_recovered_volume,

    -- penetration volumes
    sum(has_ptp_dos_in_month) as number_of_loans_with_ptp_dos,
    sum(has_ptp_dos_in_month_adj) as number_of_loans_with_ptp_dos_adj,

    -- penetration percentages (conversion tracking)
    round((sum(has_ptp_dos_in_month)::numeric / nullif(count(*), 0)) * 100, 2) as penetration_rate_pct,
    round((sum(has_ptp_dos_in_month_adj)::numeric / nullif(count(*), 0)) * 100, 2) as penetration_rate_adj_pct,

    -- arrangement fulfillment performance
    round((sum(number_of_kept_dos)::numeric / nullif(sum(number_of_ptp_dos), 0)) * 100, 2) as ptp_fulfillment_rate_pct,
    round((sum(number_of_kept_dos_adj)::numeric / nullif(sum(number_of_ptp_dos_adj), 0)) * 100, 2) as ptp_fulfillment_rate_adj_pct
from final_summary
group by 1, 2, 3
order by 1 desc, 4 desc;

