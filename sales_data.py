"""Shared validation for supplied and uploaded retail sales datasets."""

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = [
    "order_id",
    "order_date",
    "month",
    "year",
    "region",
    "category",
    "product",
    "quantity",
    "unit_price",
    "discount",
    "unit_cost",
    "revenue",
    "cost",
    "profit",
]

NUMERIC_COLUMNS = [
    "year",
    "quantity",
    "unit_price",
    "discount",
    "unit_cost",
    "revenue",
    "cost",
    "profit",
]


def validate_sales_data(data: pd.DataFrame) -> pd.DataFrame:
    missing_columns = sorted(set(REQUIRED_COLUMNS) - set(data.columns))
    if missing_columns:
        raise ValueError(f"Missing required columns: {', '.join(missing_columns)}.")

    data = data[REQUIRED_COLUMNS].copy()
    date_values = data["order_date"].astype(str)
    if not date_values.str.fullmatch(r"\d{4}-\d{2}-\d{2}").all():
        raise ValueError("order_date values must use YYYY-MM-DD.")
    data["order_date"] = pd.to_datetime(
        date_values,
        format="%Y-%m-%d",
        errors="raise",
        exact=True,
    )
    for column in NUMERIC_COLUMNS:
        data[column] = pd.to_numeric(data[column], errors="raise")

    missing = data.isna().sum()
    missing = missing[missing > 0].to_dict()
    if missing:
        raise ValueError(f"Dataset has missing values: {missing}.")
    if (data[REQUIRED_COLUMNS].select_dtypes(include=[np.number]).isin([np.inf, -np.inf])).any().any():
        raise ValueError("Dataset contains non-finite numeric values.")
    if data["order_id"].astype(str).str.strip().eq("").any():
        raise ValueError("Dataset contains an empty order_id.")
    for column in ("region", "category", "product"):
        if data[column].astype(str).str.strip().eq("").any():
            raise ValueError(f"Dataset contains an empty {column}.")
    if data["order_id"].duplicated().any():
        raise ValueError("Dataset contains duplicate order_id values.")
    if (data["quantity"] <= 0).any() or not np.equal(data["quantity"] % 1, 0).all():
        raise ValueError("Quantity values must be positive whole numbers.")
    if not np.equal(data["year"] % 1, 0).all():
        raise ValueError("Year values must be whole numbers.")
    if ((data["discount"] < 0) | (data["discount"] > 1)).any():
        raise ValueError("Discount values must be between 0 and 1.")
    if (data["unit_price"] < 0).any() or (data["unit_cost"] < 0).any():
        raise ValueError("Unit prices and unit costs cannot be negative.")
    if not (data["year"] == data["order_date"].dt.year).all():
        raise ValueError("The year column does not match order_date.")
    if not (data["month"].astype(str) == data["order_date"].dt.strftime("%Y-%m")).all():
        raise ValueError("The month column does not match order_date.")

    expected_revenue = data["quantity"] * data["unit_price"] * (1 - data["discount"])
    expected_cost = data["quantity"] * data["unit_cost"]
    expected_profit = expected_revenue - expected_cost
    for label, actual, expected in (
        ("revenue", data["revenue"], expected_revenue),
        ("cost", data["cost"], expected_cost),
        ("profit", data["profit"], expected_profit),
    ):
        if not np.allclose(actual, expected, atol=0.02, rtol=0):
            raise ValueError(f"Calculated {label} values do not match the dataset.")

    data["year"] = data["year"].astype(int)
    data["quantity"] = data["quantity"].astype(int)
    data["order_id"] = data["order_id"].astype(str)
    data["order_date"] = data["order_date"].dt.strftime("%Y-%m-%d")
    return data.sort_values(["order_date", "order_id"]).reset_index(drop=True)
