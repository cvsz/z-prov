# zeaz-web

Credential-safe web console for ZeaZ. The browser receives only bounded health
and model summaries; gateway credentials remain server-side. The initial W1
surface is a responsive provider/model/route health dashboard with no browser
storage and no third-party runtime assets.

Run the isolated profile with `docker compose --profile web up -d web`.

The console includes bounded streaming chat for all supported protocols and
read-only session, plan, approval, tool-result, audit, and execution-receipt
views. It uses no browser storage or third-party runtime assets.

Optional state paths are configured with ZEAZ_WEB_SESSION_DB,
ZEAZ_WEB_AUDIT_LOG, and ZEAZ_WEB_RECEIPTS_DIR; each must be absolute and is
read-only from the web process. Configure ZEAZ_WEB_ADMIN_KEY_HASHES with
comma-separated SHA-256 digests of a dedicated admin key to enable the
authenticated admin state endpoint. The raw admin key is never configured in
the web process or returned to the browser.
