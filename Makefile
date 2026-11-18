# Demo helpers. See docs/n8n_setup.md and docs/demo_script.md.
.PHONY: help n8n-up n8n-import demo-snapshot demo-api demo-web demo-tunnel test

N8N_CONTAINER ?= n8n
WORKFLOW := ops/n8n/workflows/alert_routing.json
PY ?= .venv/bin/python
SNAPSHOT_AS_OF ?= 2026-09-24T10:00:00Z
SNAPSHOT_DIR := data/processed/alerts_demo

help:
	@echo "make n8n-up       start n8n in Docker (localhost:5678), creating it if needed"
	@echo "make n8n-import   regenerate + import + publish the alert routing workflow"
	@echo "make demo-snapshot replay the real pipeline as of SNAPSHOT_AS_OF (quiet season: no live alerts)"
	@echo "make demo-api     run the API locally, wired to local n8n (SNAPSHOT=1 serves the snapshot)"
	@echo "make demo-web     run the dashboard against the local API"
	@echo "make demo-tunnel  expose local n8n publicly (ngrok / cloudflared) and print N8N_WEBHOOK_URL"
	@echo "make test         run the test suite"

n8n-up:
	@docker start $(N8N_CONTAINER) >/dev/null 2>&1 || \
	  docker run -d --name $(N8N_CONTAINER) -p 5678:5678 -v n8n_data:/home/node/.n8n n8nio/n8n
	@until curl -fsS -o /dev/null http://localhost:5678/healthz; do sleep 1; done
	@echo "n8n up: http://localhost:5678"

n8n-import:
	$(PY) ops/n8n/build_workflow.py
	docker cp $(WORKFLOW) $(N8N_CONTAINER):/tmp/alert_routing.json
	docker exec $(N8N_CONTAINER) n8n import:workflow --input=/tmp/alert_routing.json
	docker exec $(N8N_CONTAINER) n8n publish:workflow --id=SentinelAlertRouting
	docker restart $(N8N_CONTAINER) >/dev/null
	@until curl -fsS -o /dev/null http://localhost:5678/healthz; do sleep 1; done
	@echo "workflow live: POST http://localhost:5678/webhook/sentinel-grid/alert"

# The real pipeline (models, SHAP, Groq narration) as of a past hot day inside the 90-day
# live data window. The dashboard labels it "Snapshot, not live" and the alerts "expired".
demo-snapshot:
	$(PY) -m packages.pipelines.fusion.alerts --as-of $(SNAPSHOT_AS_OF) --out-dir $(SNAPSHOT_DIR)

demo-api:
	cd apps/api && set -a && . ../../.env && set +a && \
	  $(if $(SNAPSHOT),ALERTS_DATA_DIR=../../$(SNAPSHOT_DIR) ALERTS_STATE_DIR=../../$(SNAPSHOT_DIR)/state,) \
	  N8N_WEBHOOK_URL=http://localhost:5678/webhook/sentinel-grid/alert \
	  OPS_CALLBACK_BASE=http://host.docker.internal:8000 \
	  ../../$(PY) -m uvicorn main:app --host 0.0.0.0 --port 8000

demo-web:
	cd apps/web && NEXT_PUBLIC_API_BASE=http://localhost:8000 npm run dev

demo-tunnel:
	@bash scripts/demo_tunnel.sh

test:
	$(PY) -m pytest -q
