# Inmo Aggregator

Pequeño backend en FastAPI para buscar propiedades en la zona conurbada Tampico–Madero–Altamira.
Incluye endpoint `/search` con filtros de precio, recámaras, amueblado y tipo de inmueble.

## Cómo correr localmente

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload --port 8000
