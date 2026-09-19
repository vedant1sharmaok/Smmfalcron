"""
app.tenants — hosted bots multi-tenancy package.

Public interface:
  service.py      — tenant CRUD, user management, stats
  bot_manager.py  — TenantBotManager singleton, TenantBotInstance
  handlers.py     — build_tenant_router(tenant_id) → Router
  routes.py       — admin API routes + webhook receiver
"""
