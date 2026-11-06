# Power BI dashboard guide

The generated HTML dashboard is a companion deliverable; this guide builds the
interactive Power BI report from the same supplied data.

## 1. Load and prepare the data

In Power BI Desktop, choose **Home > Get data > Text/CSV** and select
`clean_sales.csv`. In Power Query, set `order_date` to Date; `quantity` and `year`
to Whole number; `discount` to Decimal number; and the price, cost, revenue, and
profit columns to Decimal number. Choose **Close & Apply**.

Alternatively, run `python analysis.py` and connect Power BI to
`outputs/sales.db` using a SQLite connector/ODBC driver.

## 2. Add a calendar table

In **Modeling > New table**, create:

```DAX
Calendar =
ADDCOLUMNS(
    CALENDAR(MIN(sales[order_date]), MAX(sales[order_date])),
    "Year", YEAR([Date]),
    "Month Start", DATE(YEAR([Date]), MONTH([Date]), 1),
    "Year-Month", FORMAT([Date], "yyyy-MM"),
    "Month", FORMAT([Date], "MMM")
)
```

Mark `Calendar` as the date table using its `[Date]` column. Create a one-to-many
relationship from `Calendar[Date]` to `sales[order_date]`. Sort `Calendar[Year-Month]`
by `Calendar[Month Start]`.

## 3. Create measures

```DAX
Total Revenue = SUM(sales[revenue])
Total Profit = SUM(sales[profit])
Profit Margin % = DIVIDE([Total Profit], [Total Revenue])
Orders = DISTINCTCOUNT(sales[order_id])
Units Sold = SUM(sales[quantity])
Average Order Value = DIVIDE([Total Revenue], [Orders])
Revenue LY =
    CALCULATE([Total Revenue], SAMEPERIODLASTYEAR(Calendar[Date]))
Revenue YoY % = DIVIDE([Total Revenue] - [Revenue LY], [Revenue LY])
Revenue Previous Month =
    CALCULATE([Total Revenue], PREVIOUSMONTH(Calendar[Date]))
Revenue MoM % =
    DIVIDE([Total Revenue] - [Revenue Previous Month], [Revenue Previous Month])
```

Format the margin and growth measures as percentages, and revenue/profit as ZAR.

## 4. Build the report page

| Visual | Fields |
|---|---|
| KPI cards | Total Revenue, Total Profit, Profit Margin %, Orders, Units Sold |
| Line chart | Axis: Calendar[Year-Month]; values: Total Revenue, Total Profit |
| Bar chart | Axis: product; value: Units Sold; sort descending; optional Top N filter |
| Bar chart | Axis: region; values: Total Profit and Profit Margin % |
| Donut chart | Legend: category; value: Total Revenue |
| Slicers | Calendar[Year], region, category |
| Detail table | product, Units Sold, Total Revenue, Total Profit, Profit Margin % |

Use **Edit interactions** to confirm that slicers filter every visual. Save the
report as `Sales_Dashboard.pbix`; publishing to Power BI Service is optional.
