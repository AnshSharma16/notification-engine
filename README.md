# 🚀 Distributed Notification Engine

An enterprise-grade asynchronous notification service built with **FastAPI**, **PostgreSQL**, and **Async SQLAlchemy**. The system accepts notification requests, persists them in PostgreSQL, dispatches them asynchronously using channel adapters, and tracks delivery status with retry and timeout handling.

---

## ✨ Features

- ⚡ Asynchronous FastAPI backend
- 📬 Multi-channel notification support
  - Email
  - SMS
  - Push
  - Webhook
- 🔄 Background task processing
- 🔁 Automatic retry mechanism
- 📈 Exponential Backoff
- 🎲 Jitter (avoids thundering herd)
- ⏱ Adapter timeout handling
- 🗄 PostgreSQL persistence
- 💉 Dependency Injection
- 🧩 Repository Pattern
- 🔌 Adapter Pattern
- 🛡 Global Exception Middleware
- ❤️ Health & Readiness endpoints
- 📖 Interactive Swagger documentation

---

# Tech Stack

| Category | Technology |
|-----------|------------|
| Language | Python 3.10 |
| Framework | FastAPI |
| Database | PostgreSQL |
| ORM | SQLAlchemy 2.0 (Async) |
| Validation | Pydantic v2 |
| Driver | asyncpg |
| Server | Uvicorn |
| Container | Docker |

---

# System Architecture

```
                    Client
                       │
                       ▼
                FastAPI Router
                       │
                       ▼
              Pydantic Validation
                       │
                       ▼
            Notification Service
                       │
          ┌────────────┴────────────┐
          │                         │
          ▼                         ▼
   Notification Repository    Background Task
          │                         │
          ▼                         ▼
      PostgreSQL              Retry Manager
                                        │
                                        ▼
                              Exponential Backoff
                                        │
                                        ▼
                                Timeout Wrapper
                                        │
                                        ▼
                                 Channel Adapter
                           ┌────────┼─────────┐
                           ▼        ▼         ▼
                         Email     SMS      Webhook
```

---

# Notification Lifecycle

```
PENDING
    │
    ▼
PROCESSING
    │
 ┌──┴─────────────┐
 ▼                ▼
DELIVERED      FAILED
                  │
                  ▼
             RETRYING
```

---

# API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/notify` | Create a notification |
| GET | `/api/v1/notify` | List notifications |
| GET | `/api/v1/notify/{notification_id}` | Get notification status |
| GET | `/health` | Health check |
| GET | `/ready` | Readiness check |

---

# Request Flow

```
Client
   │
POST /notify
   │
   ▼
FastAPI Router
   │
   ▼
Validate Request
   │
   ▼
Repository
   │
   ▼
PostgreSQL (PENDING)
   │
   ▼
HTTP 202 Accepted
   │
   ▼
Background Task
   │
   ▼
PROCESSING
   │
   ▼
Retry Manager
   │
   ▼
Adapter.send()
   │
   ├────────► DELIVERED
   ├────────► FAILED
   └────────► RETRYING
```

---

# Reliability Features

- Asynchronous processing
- Retry Manager
- Exponential Backoff
- Randomized Jitter
- Adapter Timeout Protection
- Connection Pooling
- Graceful Cancellation Handling
- Status Tracking
- Global Exception Handling

---

# Design Patterns

### Repository Pattern

Separates SQL queries from business logic.

```
Router
   │
   ▼
Service
   │
   ▼
Repository
   │
   ▼
Database
```

---

### Adapter Pattern

Supports multiple notification providers without changing business logic.

```
NotificationService
        │
        ▼
 Adapter Factory
        │
 ┌──────┼──────────┐
 ▼      ▼          ▼
Email   SMS     Webhook
```

---

### Dependency Injection

FastAPI automatically injects the required services instead of creating them manually.

---

# Running Locally

## Clone Repository

```bash
git clone https://github.com/AnshSharma16/notification-engine.git
cd notification-engine
```

## Install Dependencies

```bash
pip install -r requirements.txt
```

## Run PostgreSQL

```bash
docker run -d \
--name notification-postgres \
-e POSTGRES_USER=postgres \
-e POSTGRES_PASSWORD=postgres \
-e POSTGRES_DB=notifications \
-p 5434:5432 \
postgres:16
```

## Start the Server

```bash
uvicorn main:app --reload
```

Swagger UI

```
http://127.0.0.1:8000/docs
```

---

# Project Structure

```
notification_engine/

├── main.py
├── services.py
├── repositories.py
├── database.py
├── models.py
├── schemas.py
├── middleware.py
├── config.py
├── requirements.txt
└── README.md
```

---

# Future Improvements

- Alembic migrations
- Docker Compose
- Redis Queue
- Celery Workers
- RabbitMQ / Kafka
- Prometheus Metrics
- Grafana Dashboard
- OpenTelemetry Tracing
- Unit & Integration Tests
- GitHub Actions CI/CD
- Kubernetes Deployment

---

# Backend Concepts Demonstrated

- Async Programming
- FastAPI Background Tasks
- Async SQLAlchemy
- PostgreSQL
- Retry Mechanism
- Exponential Backoff
- Jitter
- Timeout Handling
- Graceful Shutdown
- Repository Pattern
- Adapter Pattern
- Dependency Injection
- Middleware
- Connection Pooling
- REST API Design

---

# Author

**Ansh Sharma**

- Python Backend Developer
- FastAPI
- PostgreSQL
- AsyncIO
- Automation
- Machine Learning

GitHub: https://github.com/AnshSharma16
