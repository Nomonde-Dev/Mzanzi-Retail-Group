"""Analyze the provided retail sales CSV and build Excel, SQLite, and HTML outputs.

Run from this folder with:
    python -m pip install -r requirements.txt
    python analysis.py
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from sales_data import validate_sales_data


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "clean_sales.csv"
OUTPUT = ROOT / "outputs"
TEMPLATE = ROOT / "dashboard_template.html"

QUERIES = {
    "best_selling_products": """
        SELECT product, category, SUM(quantity) AS units_sold,
               ROUND(SUM(revenue), 2) AS revenue,
               ROUND(SUM(profit), 2) AS profit
        FROM sales
        GROUP BY product, category
        ORDER BY units_sold DESC;
    """,
    "profitable_regions": """
        SELECT region, ROUND(SUM(revenue), 2) AS revenue,
               ROUND(SUM(profit), 2) AS profit,
               ROUND(100.0 * SUM(profit) / NULLIF(SUM(revenue), 0), 1) AS margin_pct
        FROM sales
        GROUP BY region
        ORDER BY profit DESC;
    """,
    "monthly_trend": """
        SELECT month, ROUND(SUM(revenue), 2) AS revenue,
               ROUND(SUM(profit), 2) AS profit,
               COUNT(DISTINCT order_id) AS orders
        FROM sales
        GROUP BY month
        ORDER BY month;
    """,
    "yearly_growth": """
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
        ORDER BY year;
    """,
    "category_by_region": """
        SELECT region, category, ROUND(SUM(revenue), 2) AS revenue,
               ROUND(SUM(profit), 2) AS profit
        FROM sales
        GROUP BY region, category
        ORDER BY region, profit DESC;
    """,
    "discount_impact": """
        SELECT discount, COUNT(DISTINCT order_id) AS orders,
               ROUND(AVG(profit), 2) AS avg_profit_per_order,
               ROUND(100.0 * SUM(profit) / NULLIF(SUM(revenue), 0), 1) AS margin_pct
        FROM sales
        GROUP BY discount
        ORDER BY discount;
    """,
}


def load_sales_data() -> pd.DataFrame:
    if not SOURCE.is_file():
        raise FileNotFoundError(f"Required source data was not found: {SOURCE}")

    data = validate_sales_data(pd.read_csv(SOURCE))
    data["order_date"] = pd.to_datetime(data["order_date"])
    return data


def create_excel_report(data: pd.DataFrame, output_path) -> None:
    products = (
        data.groupby(["product", "category"], as_index=False)
        .agg(
            units_sold=("quantity", "sum"),
            orders=("order_id", "nunique"),
            revenue=("revenue", "sum"),
            profit=("profit", "sum"),
        )
    )
    products["margin_pct"] = products["profit"] / products["revenue"]
    products = products.sort_values(["units_sold", "revenue"], ascending=False)

    regions = (
        data.groupby("region", as_index=False)
        .agg(
            orders=("order_id", "nunique"),
            units_sold=("quantity", "sum"),
            revenue=("revenue", "sum"),
            profit=("profit", "sum"),
        )
    )
    regions["margin_pct"] = regions["profit"] / regions["revenue"]
    regions = regions.sort_values("profit", ascending=False)

    monthly = (
        data.groupby("month", as_index=False)
        .agg(
            orders=("order_id", "nunique"),
            revenue=("revenue", "sum"),
            profit=("profit", "sum"),
        )
        .sort_values("month")
    )
    monthly["revenue_mom_growth_pct"] = monthly["revenue"].pct_change()

    yearly = (
        data.groupby("year", as_index=False)
        .agg(
            orders=("order_id", "nunique"),
            units_sold=("quantity", "sum"),
            revenue=("revenue", "sum"),
            profit=("profit", "sum"),
        )
        .sort_values("year")
    )
    yearly["revenue_growth_pct"] = yearly["revenue"].pct_change()

    order_count = data["order_id"].nunique()
    total_revenue = data["revenue"].sum()
    total_profit = data["profit"].sum()
    top_product = products.iloc[0]
    top_region = regions.iloc[0]
    highest_margin_region = regions.loc[regions["margin_pct"].idxmax()]
    summary = pd.DataFrame(
        [
            ("Total revenue (ZAR)", total_revenue),
            ("Total profit (ZAR)", total_profit),
            ("Overall profit margin", total_profit / total_revenue),
            ("Orders", order_count),
            ("Units sold", data["quantity"].sum()),
            ("Average order value (ZAR)", total_revenue / order_count),
            ("Best-selling product (units)", top_product["product"]),
            ("Most profitable region (ZAR profit)", top_region["region"]),
            ("Highest-margin region", highest_margin_region["region"]),
            ("Analysis period", f"{data['year'].min()}-{data['year'].max()}"),
        ],
        columns=["Metric", "Value"],
    )

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        excel_safe_frame(summary).to_excel(writer, sheet_name="Executive Summary", index=False)
        excel_safe_frame(products).to_excel(writer, sheet_name="Products", index=False)
        excel_safe_frame(regions).to_excel(writer, sheet_name="Regions", index=False)
        excel_safe_frame(monthly).to_excel(writer, sheet_name="Monthly Trends", index=False)
        excel_safe_frame(yearly).to_excel(writer, sheet_name="Yearly Trends", index=False)
        excel_safe_frame(data).to_excel(writer, sheet_name="Clean Data", index=False)

    if hasattr(output_path, "seek"):
        output_path.seek(0)
    workbook = load_workbook(output_path)
    header_fill = PatternFill("solid", fgColor="1F3A5F")
    for worksheet in workbook.worksheets:
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for cell in worksheet[1]:
            cell.fill = header_fill
            cell.font = Font(name="Arial", bold=True, color="FFFFFF")
            cell.alignment = Alignment(horizontal="center")
        for column_cells in worksheet.columns:
            column_letter = column_cells[0].column_letter
            header = str(column_cells[0].value or "")
            max_length = max(len(str(cell.value or "")) for cell in column_cells[:200])
            worksheet.column_dimensions[column_letter].width = min(max(max_length + 2, 14), 30)
            for cell in column_cells[1:]:
                if "margin_pct" in header or "growth_pct" in header or (
                    worksheet.title == "Executive Summary"
                    and header == "Value"
                    and cell.row == 4
                ):
                    cell.number_format = "0.0%"
                elif any(token in header.lower() for token in ("revenue", "profit", "cost", "price")):
                    cell.number_format = '"R" #,##0.00;[Red]("R" #,##0.00)'
        if worksheet.title == "Clean Data":
            for cell in worksheet["B"][1:]:
                cell.number_format = "yyyy-mm-dd"
    if hasattr(output_path, "seek"):
        output_path.seek(0)
        output_path.truncate(0)
    workbook.save(output_path)
    if hasattr(output_path, "seek"):
        output_path.seek(0)


def excel_safe_frame(data: pd.DataFrame) -> pd.DataFrame:
    safe_data = data.copy()
    for column in safe_data.select_dtypes(include=["object", "string"]).columns:
        safe_data[column] = safe_data[column].map(
            lambda value: f"'{value}"
            if isinstance(value, str)
            and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@"))
            else value
        )
    return safe_data


def create_dashboard(data: pd.DataFrame, output_path: Path) -> None:
    rows = []
    for row in data.itertuples(index=False):
        rows.append(
            {
                "order_date": row.order_date.strftime("%Y-%m-%d"),
                "month": row.month,
                "year": int(row.year),
                "region": row.region,
                "category": row.category,
                "product": row.product,
                "quantity": int(row.quantity),
                "unit_price": round(float(row.unit_price), 2),
                "discount": round(float(row.discount), 4),
                "unit_cost": round(float(row.unit_cost), 2),
                "revenue": round(float(row.revenue), 2),
                "cost": round(float(row.cost), 2),
                "profit": round(float(row.profit), 2),
                "order_id": row.order_id,
            }
        )

    if not TEMPLATE.is_file():
        raise FileNotFoundError(f"Dashboard template was not found: {TEMPLATE}")
    template = TEMPLATE.read_text(encoding="utf-8")
    dashboard = template.replace(
        "__SALES_DATA__",
        json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029"),
    )
    dashboard = dashboard.replace("__SERVER_MODE__", "false")
    dashboard = dashboard.replace("__CSRF_TOKEN__", "")
    dashboard = dashboard.replace("__CSP_NONCE__", "")
    dashboard = dashboard.replace("__USER_NAME__", "Mzanzi Retail Group")
    dashboard = dashboard.replace("__USER_ROLE__", "Demo")
    dashboard = dashboard.replace("__USER_DISPLAY_NAME__", "Demo User")
    dashboard = dashboard.replace("__ADMIN_NAV__", "")
    dashboard = dashboard.replace(
        "__REFRESHED_AT__",
        datetime.now().astimezone().isoformat(timespec="seconds"),
    )
    output_path.write_text(dashboard, encoding="utf-8")


def main() -> None:
    data = load_sales_data()
    OUTPUT.mkdir(parents=True, exist_ok=True)

    sql_path = ROOT / "queries.sql"
    with sql_path.open("w", encoding="utf-8", newline="\n") as sql_file:
        sql_file.write("-- Run against outputs/sales.db (table: sales).\n\n")
        for name, query in QUERIES.items():
            sql_file.write(f"-- {name}\n{query.strip()};\n\n")

    database_path = OUTPUT / "sales.db"
    database_data = data.copy()
    database_data["order_date"] = database_data["order_date"].dt.strftime("%Y-%m-%d")
    with sqlite3.connect(database_path) as connection:
        database_data.to_sql("sales", connection, if_exists="replace", index=False)
        results = {
            name: pd.read_sql_query(query, connection)
            for name, query in QUERIES.items()
        }

    report_path = OUTPUT / "Sales_Analysis_Report.xlsx"
    create_excel_report(data, report_path)
    dashboard_path = OUTPUT / "dashboard.html"
    create_dashboard(data, dashboard_path)

    print(f"Source rows: {len(data):,} | Unique orders: {data['order_id'].nunique():,}")
    print(
        f"Revenue: R{data['revenue'].sum():,.2f} | "
        f"Profit: R{data['profit'].sum():,.2f} | "
        f"Margin: {data['profit'].sum() / data['revenue'].sum():.1%}"
    )
    print("\nBest-selling products by units:")
    print(results["best_selling_products"].head(5).to_string(index=False))
    print("\nRegions by profit:")
    print(results["profitable_regions"].to_string(index=False))
    print("\nYearly trend:")
    print(results["yearly_growth"].to_string(index=False))
    print(f"\nCreated: {report_path}")
    print(f"Created: {database_path}")
    print(f"Created: {dashboard_path}")


if __name__ == "__main__":
    main()
