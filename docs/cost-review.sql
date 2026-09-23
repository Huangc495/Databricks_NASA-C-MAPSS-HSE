-- List prices in USD, not the actual Azure invoice or negotiated rate.
-- Billing ingestion is delayed; this cannot enforce a real-time $10/day cutoff.
-- Review Azure infrastructure (storage/networking) in Azure Cost Management too.
SELECT
  u.usage_date,
  u.usage_metadata.job_id,
  u.sku_name,
  SUM(u.usage_quantity) AS dbus,
  SUM(u.usage_quantity * p.pricing.default) AS estimated_list_cost_usd
FROM system.billing.usage u
JOIN system.billing.list_prices p
  ON u.cloud = p.cloud
  AND u.sku_name = p.sku_name
  AND u.usage_unit = p.usage_unit
  AND u.usage_start_time >= p.price_start_time
  AND (u.usage_end_time <= p.price_end_time OR p.price_end_time IS NULL)
WHERE u.workspace_id = '7405619144539463'
  AND u.usage_date >= current_date() - INTERVAL 7 DAYS
  AND p.currency_code = 'USD'
GROUP BY u.usage_date, u.usage_metadata.job_id, u.sku_name
ORDER BY u.usage_date DESC;
