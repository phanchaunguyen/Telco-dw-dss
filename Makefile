# ===========================================================================
#  Medallion Data Warehouse — Makefile
#  Every psql call goes through the running container, so no host psql needed.
# ===========================================================================
SHELL := /bin/bash
.DEFAULT_GOAL := help

# Load .env if present (ports, credentials, HF dataset coordinates)
-include .env
export

DB_USER       ?= dw
DB_PASSWORD   ?= dw
DB_NAME       ?= warehouse
DB_HOST       ?= localhost
DB_PORT       ?= 5432
POSTGRES_PORT ?= 5432
METABASE_PORT ?= 3000

PY  := .venv/bin/python
PIP := .venv/bin/pip

# -T disables TTY allocation (required for non-interactive make)
# -v ON_ERROR_STOP=1 makes psql exit non-zero on the first SQL error,
# otherwise `make` would report success on a failed script.
PSQL := docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U $(DB_USER) -d $(DB_NAME)

.PHONY: help install up down reset reset-soft wait-db init-db \
        download profile bronze silver-gold marts refresh-mv \
        olap-demo validate export all clean

# ---------------------------------------------------------------------------
help:  ## Show this help
	@echo "Medallion DW — available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
install:  ## Create .venv and install pinned dependencies
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example")

up:  ## Start Postgres + Metabase and wait until healthy
	docker compose up -d --wait
	@echo "Postgres : localhost:$(POSTGRES_PORT)"
	@echo "Metabase : http://localhost:$(METABASE_PORT)"

down:  ## Stop containers (keeps volumes, keeps Metabase dashboards)
	docker compose down

reset:  ## DESTRUCTIVE: stop containers and delete volumes (data + dashboards)
	docker compose down -v
	@echo "Volumes removed. data/raw/ CSV is NOT touched."

reset-soft:  ## Truncate bronze/silver/gold, keep containers and dashboards
	@# TRUNCATE has no wildcard syntax, and every FK-related table must be
	@# listed in ONE statement. No CASCADE: a new referencing table makes this
	@# fail loudly instead of being silently wiped. RESTART IDENTITY keeps
	@# surrogate keys reproducible across rebuilds.
	$(PSQL) -c "DO \$$\$$ DECLARE t text; existing text[] := '{}'; \
	  candidates text[] := ARRAY['gold.fact_billing_support','gold.dim_customer', \
	    'gold.dim_date','gold.dim_payment_status','gold.dim_issue_type', \
	    'gold.dim_support_channel','gold.dq_results', \
	    'silver.silver_customer_billing','bronze.bronze_customer_billing']; \
	  BEGIN \
	    FOREACH t IN ARRAY candidates LOOP \
	      IF to_regclass(t) IS NOT NULL THEN existing := existing || t; END IF; \
	    END LOOP; \
	    IF array_length(existing,1) IS NULL THEN \
	      RAISE NOTICE 'reset-soft: nothing to truncate'; RETURN; \
	    END IF; \
	    EXECUTE 'TRUNCATE TABLE ' || array_to_string(existing, ', ') || ' RESTART IDENTITY'; \
	  END \$$\$$;"
	@# Materialized views are not tables: TRUNCATE does not apply, and
	@# CREATE MATERIALIZED VIEW IF NOT EXISTS will not repopulate them.
	$(PSQL) -c "DO \$$\$$ BEGIN \
	  IF to_regclass('gold.mv_billing_monthly') IS NOT NULL THEN \
	    REFRESH MATERIALIZED VIEW gold.mv_billing_monthly; END IF; \
	  IF to_regclass('gold.mv_churn_by_issue_channel') IS NOT NULL THEN \
	    REFRESH MATERIALIZED VIEW gold.mv_churn_by_issue_channel; END IF; \
	  END \$$\$$;"
	@echo "reset-soft: warehouse truncated, containers + Metabase preserved."

wait-db:  ## Block until Postgres accepts connections
	@for i in $$(seq 1 60); do \
	  if docker compose exec -T postgres pg_isready -U $(DB_USER) -d $(DB_NAME) >/dev/null 2>&1; then \
	    echo "Postgres is ready."; exit 0; \
	  fi; \
	  sleep 1; \
	done; \
	echo "Postgres did not become ready in 60s" >&2; exit 1

init-db:  ## Create bronze/silver/gold schemas and gold.dq_results
	$(PSQL) -f sql/00_init_schemas.sql

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
download:  ## Download the HF dataset into data/raw/telco_churn.csv
	$(PY) scripts/download_dataset.py

profile:  ## Profile the raw CSV and write data/profile.txt
	$(PY) src/00_profile.py

bronze: init-db  ## Extract + Load: CSV -> bronze (append-only)
	$(PY) src/01_load_bronze.py

silver-gold:  ## ELT: bronze -> silver -> gold (all transforms in SQL)
	$(PY) src/02_run_sql.py

marts:  ## Create mart_* views and mv_* materialized views
	$(PY) src/03_build_marts.py

refresh-mv:  ## Refresh materialized views CONCURRENTLY
	$(PSQL) -c "REFRESH MATERIALIZED VIEW CONCURRENTLY gold.mv_billing_monthly;"
	$(PSQL) -c "REFRESH MATERIALIZED VIEW CONCURRENTLY gold.mv_churn_by_issue_channel;"

olap-demo:  ## Run the 5 OLAP demonstrations (+ CUBE bonus)
	$(PSQL) -f sql/05_olap_demos.sql

validate:  ## Run data-quality rules, write gold.dq_results
	$(PY) src/04_validate.py

export:  ## Export marts, dimensions and denormalized fact to CSV + Parquet
	$(PY) src/05_export_marts.py

all: up download profile bronze silver-gold marts refresh-mv validate export  ## Full pipeline
	@echo "Pipeline finished. Run 'make olap-demo' to see the OLAP queries."

clean:  ## Remove Python caches and generated exports (keeps data/raw CSV)
	rm -rf __pycache__ src/__pycache__ scripts/__pycache__ .pytest_cache
	rm -f exports/*.csv exports/*.parquet
	@echo "Cleaned caches and exports."
