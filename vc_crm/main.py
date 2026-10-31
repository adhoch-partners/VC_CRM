"""ASGI entry point: `uvicorn vc_crm.main:app`. Database path comes from VC_CRM_DB."""

from .app import create_app

app = create_app(start_jobs=True)
