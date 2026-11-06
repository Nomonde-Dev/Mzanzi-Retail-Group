-- Run against outputs/sales.db (table: sales).

-- best_selling_products
SELECT product, category, SUM(quantity) AS units_sold,
               ROUND(SUM(revenue), 2) AS revenue,
               ROUND(SUM(profit), 2) AS profit
        FROM sales
        GROUP BY product, category
        ORDER BY units_sold DESC;;

-- profitable_regions
SELECT region, ROUND(SUM(revenue), 2) AS revenue,
               ROUND(SUM(profit), 2) AS profit,
               ROUND(100.0 * SUM(profit) / NULLIF(SUM(revenue), 0), 1) AS margin_pct
        FROM sales
        GROUP BY region
        ORDER BY profit DESC;;

-- monthly_trend
SELECT month, ROUND(SUM(revenue), 2) AS revenue,
               ROUND(SUM(profit), 2) AS profit,
               COUNT(DISTINCT order_id) AS orders
        FROM sales
        GROUP BY month
        ORDER BY month;;

-- yearly_growth
WITH yearly AS (
            SELECT year, SUM(revenue) AS revenue, SUM(profit) AS profit,
                   COUNT(DISTINCT order_id) AS orders
            FROM sales
            GROUP BY year
        )
        SELECT year, ROUND(revenue, 2) AS revenue, ROUND(profit, 2) AS profit,
               orders,
               ROUND(
                   100.0 * (revenue - LAG(revenue) OVER (ORDER BY year))
                   / NULLIF(LAG(revenue) OVER (ORDER BY year), 0),
                   1
               ) AS revenue_growth_pct
        FROM yearly
        ORDER BY year;;

-- category_by_region
SELECT region, category, ROUND(SUM(revenue), 2) AS revenue,
               ROUND(SUM(profit), 2) AS profit
        FROM sales
        GROUP BY region, category
        ORDER BY region, profit DESC;;

-- discount_impact
SELECT discount, COUNT(DISTINCT order_id) AS orders,
               ROUND(AVG(profit), 2) AS avg_profit_per_order,
               ROUND(100.0 * SUM(profit) / NULLIF(SUM(revenue), 0), 1) AS margin_pct
        FROM sales
        GROUP BY discount
        ORDER BY discount;;

