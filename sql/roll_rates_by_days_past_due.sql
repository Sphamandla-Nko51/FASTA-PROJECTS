
with params AS (SELECT DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '1 month' AS reporting_month),
     master as (
    select fda.loannumber,
       fda.reporting_month,
       mdpd.reporting_month_end,
       fda.department_start_of_month,
       coalesce(fda.dept_at_end,fda.department_start_of_month) as department_end_of_month,
       fda.department,
       fda.department_end,
       mdpd.snap_date,
       mdpd.month_index,
       mdpd.days_past_due,
       mdpd.dpd,
       mdpd.dpd_at_start,
       mdpd.status,
       fda.opening_balance,
       fda.instalment_due_in_month,
       mds.opening_arrears,
       (fda.instalment_due_in_month + mds.opening_arrears) as total_arrears,
       mds.closing_arrears,
       fda.net_receipts,
       fda.closing_balance,
       mds.opening_is_active,
       mds.in_term_flag,
       mds.delinquency_segment,
       mds.delinquency_bucket,
       mds.product,
--        mds.true_days_past_due,
--        mds.oldest_unpaid_instalment_date,
       mds.is_fpd10,
       mds.is_fpd30,
       mds.is_written_off,
       mds.prev_months_past_maturity,
       mds.prev_delinquency_segment,
       mds.prev_delinquency_bucket, case
        when mdpd.DPD_At_Start = '0.ANew'  then '0.New'
        when mdpd.DPD = '0.Current' and mdpd.DPD_At_Start = '0.Current' then '1.Current'
        when mdpd.DPD = '0.Current' and mdpd.DPD_At_Start not in ('0.Current') then '2.Cured'
        when mdpd.DPD = '7.91DPD' and mdpd.DPD_At_Start = '7.91DPD' then '6.Default'
        when mdpd.DPD = mdpd.DPD_At_Start then '4.Stable'
        when mdpd.DPD > mdpd.DPD_At_Start then '5.Rolled Forward'
        else '3.Rolled Backward'
        end as  movement_type

from fasta_views.mv_department_start_of_month fda
left join fasta_views.mv_delinquency_segments mds on fda.loannumber = mds.loannumber
                                                         and fda.reporting_month = mds.transaction_month
left join fasta_views.mv_days_past_due mdpd on fda.loannumber = mdpd.loannumber
                                                         and fda.reporting_month = mdpd.reporting_month
where fda.reporting_month::date between
        (select reporting_month from params) - interval '13 months'
    and (select reporting_month from params)

--     and fda.loannumber = '0000068329'
)

select
        snap_date,
        reporting_month_end,
       dpd_at_start as dpd_at_start_of_month,
       dpd as dpd_at_end_of_month,
       movement_type,
       count(*) as loan_count
from master
group by 1, 2, 3, 4,5
order by 1, 3, 4, 5;