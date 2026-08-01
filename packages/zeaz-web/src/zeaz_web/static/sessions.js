const esc = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const byId = (id) => document.getElementById(id);
const eventHtml = (item) => '<article class="event"><strong>' + esc(item.event_type || item.role) + '</strong> · ' + esc(item.session_id) + '<br><small>' + esc(item.created_at || "") + '</small><pre>' + esc(JSON.stringify(item.details || item.blocks || {}, null, 2)) + '</pre></article>';
async function loadState() {
  const status = byId("state-status");
  try {
    const response = await fetch("/api/state", {headers: {accept: "application/json"}, cache: "no-store"});
    if (!response.ok) throw new Error("request failed");
    const data = await response.json();
    byId("sessions").innerHTML = (data.sessions || []).map((s) => '<article class="card"><strong>' + esc(s.id) + '</strong><br>' + esc(s.status) + ' · ' + esc(s.execution_mode) + '<br><small>' + esc(s.turn_count) + ' turns · rev ' + esc(s.revision) + '</small></article>').join("") || '<p class="muted">No sessions available.</p>';
    byId("decisions").innerHTML = (data.plans || []).concat(data.approvals || []).map(eventHtml).join("") || '<p class="muted">No plan or approval events.</p>';
    byId("turns").innerHTML = (data.turns || []).filter((t) => (t.blocks || []).some((b) => b.type === "tool_result" || b.type === "tool_call")).map(eventHtml).join("") || '<p class="muted">No tool activity.</p>';
    byId("audit").innerHTML = (data.audit || []).map(eventHtml).join("") || '<p class="muted">No audit events.</p>';
    byId("receipts").innerHTML = (data.receipts || []).map((r) => '<article class="card"><strong>' + esc(r.id) + '</strong><br>' + esc(r.state) + ' · cleanup ' + esc(r.cleanup_complete) + '<br><small>' + esc(r.image_digest) + '</small></article>').join("") || '<p class="muted">No receipts available.</p>';
    status.textContent = (data.warnings || []).join(" · ") || "State loaded.";
  } catch (error) { status.textContent = "State unavailable."; }
}
loadState();
