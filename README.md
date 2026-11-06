# Mzanzi Retail Group

## Business Intelligence & Analytics

**Smarter Insights. Better Decisions.**  
Transforming retail data into meaningful business insights.

A web-based retail business intelligence application for analysing sales
performance, monitoring profitability, identifying product demand, and supporting
data-driven decisions. Project type: **Data Analytics + Web Application Development**.

The supplied CSV contains sample/demo records; do not treat its results as actual
Mzanzi Retail Group business results.

## Start the authenticated web application (Windows PowerShell)

Run these commands from the project folder. Use the same secret value each time
you restart the app so signed-in sessions remain valid.

```powershell
Set-Location 'C:\Users\Admin\Downloads\files (4)'
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if (-not [Environment]::GetEnvironmentVariable('FLASK_SECRET_KEY', 'User')) {
    $key = (& '.\.venv\Scripts\python.exe' -c "import secrets; print(secrets.token_hex(32))").Trim()
    [Environment]::SetEnvironmentVariable('FLASK_SECRET_KEY', $key, 'User')
}
$env:FLASK_SECRET_KEY = [Environment]::GetEnvironmentVariable('FLASK_SECRET_KEY', 'User')
& '.\.venv\Scripts\python.exe' app.py create-admin --email admin@example.com --name "Mzanzi Administrator"
& '.\.venv\Scripts\python.exe' app.py run
```

The administrator setup prompts you to enter and confirm a password. Choose a
unique password of at least 12 characters. The generated `FLASK_SECRET_KEY` is
saved as a current-user environment variable so the app can reuse it after
restarting. Keep it private. The local
application opens at [http://127.0.0.1:5000](http://127.0.0.1:5000). Stop it with
Ctrl+C.

Create the first administrator only once. The SQLite application database is
stored at `outputs/mzanzi_app.db`. Administrator accounts can create analyst and
administrator accounts and reset passwords from **Manage Users**. Passwords are
stored as salted hashes; there is no default or shared login.

You can also select **Create an account** on the sign-in page. Self-registered
accounts are Analysts; an administrator account must be created with the
one-time command above or by an existing administrator.

## Roles

| Capability | Administrator | Analyst |
|---|---:|---:|
| View dashboard, products, regions, and insights | Yes | Yes |
| Upload validated sales CSV datasets | Yes | Yes |
| Select an active dataset | Yes | Yes |
| Generate/download Excel and CSV reports | Yes | Yes |
| Manage user accounts and reset passwords | Yes | No |
| Permanently delete uploaded datasets | Yes | No |

## Analytics and deliverables

The authenticated application provides:

- KPI cards with calculated previous-year comparisons.
- Date, year, region, category, and product filters.
- Revenue, profit, product, category, and regional charts.
- Calculated business insights and transaction-level correlations.
- Searchable, sortable, paginated transactions and report downloads.
- Browser CSV upload with server-side validation and persistent SQLite storage.
- An administrator-managed user list and role-protected dataset deletion.

Run the standalone analysis pipeline separately when you want to rebuild the
Excel workbook, SQL database, and offline dashboard:

```powershell
& '.\.venv\Scripts\python.exe' analysis.py
```

It generates:

- `outputs/Sales_Analysis_Report.xlsx`
- `outputs/sales.db`
- `outputs/dashboard.html` — an offline demo copy, not the authenticated app.
- `queries.sql` — SQL analyses for `outputs/sales.db`.

Use the authenticated web app at `http://127.0.0.1:5000`; do not open the offline
HTML file when you need login or role enforcement.

## Security and deployment notes

The app uses password hashing, signed HTTP-only SameSite cookies, CSRF tokens,
login-attempt throttling, input validation, and server-side role checks. It binds
to `127.0.0.1` by default. This is a local learning project, not a production
deployment recipe. Before exposing it to a network, deploy behind HTTPS, set
`COOKIE_SECURE=1`, configure a production WSGI server and appropriate host/proxy
settings, protect the database and secret, and review operational monitoring and
backup requirements. Password recovery is administrator-managed; email delivery
is not configured.

## Power BI

For the Power BI model, DAX measures, and report layout, see
[POWERBI_GUIDE.md](./POWERBI_GUIDE.md).
