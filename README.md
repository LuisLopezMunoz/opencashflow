# OpenCashFlow

A multi-platform personal cash flow management application designed for household and personal finance management. Run it as a web server on Linux (or any platform) and access it from any device with a browser.

## Features

- **Multi-wallet / diary support** — cash, bank accounts, savings, investments
- **Transaction tracking** — income, expenses, and transfers with category tags
- **Credit card management** — track balances, limits, charges, closing/due dates
- **Bank loan tracking** — principal, remaining balance, interest rate, monthly payments, payment history
- **Multiple income sources** — salary, freelance, rental, investments with frequency support
- **Financial summary dashboard** — total balances, net monthly cash flow at a glance
- **Secure user accounts** — JWT authentication, bcrypt passwords
- **REST API** — fully documented at `/docs` (Swagger UI)
- **Linux server ready** — runs via Docker or directly with uvicorn

## Quick Start

### Option 1: Docker (recommended for Linux server)

```bash
# Clone and start
git clone https://github.com/LuisLopezMunoz/opencashflow.git
cd opencashflow

# Set a strong secret key
export OCF_SECRET_KEY=your-very-strong-secret-key

# Start the server
docker compose up -d

# Open in browser
open http://localhost:8000
```

### Option 2: Run directly with Python

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000

# Open in browser
open http://localhost:8000
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OCF_SECRET_KEY` | `opencashflow-dev-secret-change-in-production` | JWT signing secret — **change this in production** |
| `DATABASE_URL` | `sqlite:///./opencashflow.db` | SQLAlchemy database URL |

## API Documentation

Interactive API docs are available at:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

### Endpoints

| Resource | Endpoints |
|---|---|
| Auth | `POST /api/auth/register`, `POST /api/auth/token`, `GET /api/auth/me` |
| Wallets | `GET/POST /api/wallets/`, `GET/PUT/DELETE /api/wallets/{id}` |
| Transactions | `GET/POST /api/transactions/`, `GET/PUT/DELETE /api/transactions/{id}` |
| Credit Cards | `GET/POST /api/credit-cards/`, `GET/PUT/DELETE /api/credit-cards/{id}` |
| CC Charges | `GET/POST /api/credit-cards/{id}/charges`, `PUT/DELETE /api/credit-cards/{id}/charges/{charge_id}` |
| Loans | `GET/POST /api/loans/`, `GET/PUT/DELETE /api/loans/{id}` |
| Loan Payments | `GET/POST /api/loans/{id}/payments`, `PUT/DELETE /api/loans/{id}/payments/{pay_id}` |
| Income Sources | `GET/POST /api/income-sources/`, `GET/PUT/DELETE /api/income-sources/{id}` |
| Summary | `GET /api/summary/` |

## Running Tests

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

## Project Structure

```
opencashflow/
├── backend/
│   ├── main.py           # FastAPI app entry point
│   ├── database.py       # SQLAlchemy setup
│   ├── auth.py           # JWT / password utilities
│   ├── dependencies.py   # FastAPI dependencies
│   ├── models/           # SQLAlchemy ORM models
│   ├── routers/          # API route handlers
│   └── schemas/          # Pydantic request/response schemas
├── frontend/
│   ├── index.html        # Single-page web application
│   └── static/
│       ├── css/style.css
│       └── js/app.js
├── tests/                # pytest test suite
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
