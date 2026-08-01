const servicesEl = document.querySelector('#services');
const modelsEl = document.querySelector('#models');
const routesEl = document.querySelector('#routes');
const updatedEl = document.querySelector('#last-updated');
const countEl = document.querySelector('#model-count');

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}

function renderServices(services) {
  servicesEl.innerHTML = services.map((service) => `
    <article class="service-card">
      <div class="service-name"><span>${escapeHtml(service.name)}</span><span class="status ${escapeHtml(service.status)}">${escapeHtml(service.status)}</span></div>
      <div class="service-url" title="${escapeHtml(service.url)}">${escapeHtml(service.url)}</div>
      <div class="service-latency">${service.latency_ms === null ? '—' : escapeHtml(service.latency_ms)} <small>${service.latency_ms === null ? '' : 'ms response'}</small></div>
    </article>`).join('');
}

function renderModels(models) {
  countEl.textContent = `${models.length} route${models.length === 1 ? '' : 's'}`;
  if (!models.length) { modelsEl.innerHTML = '<div class="empty">No model routes reported by the gateway.</div>'; return; }
  modelsEl.innerHTML = `<table><thead><tr><th>Model alias</th><th>Provider</th><th>Route</th></tr></thead><tbody>${models.map((model) => `<tr><td><strong>${escapeHtml(model.id)}</strong></td><td>${escapeHtml(model.owned_by)}</td><td><span class="route-pill">${escapeHtml(model.route)}</span></td></tr>`).join('')}</tbody></table>`;
}

function renderRoutes(routes) {
  routesEl.innerHTML = routes.length ? routes.map((route) => `
    <article class="service-card">
      <div class="service-name"><span>${escapeHtml(route.name)}</span><span class="status ${escapeHtml(route.status)}">${escapeHtml(route.status)}</span></div>
      <div class="service-url">Provider route</div>
      <div class="service-latency">${escapeHtml(route.model_count)} <small>model${route.model_count === 1 ? '' : 's'}</small></div>
    </article>`).join('') : '<div class="empty">No provider routes reported.</div>';
}

async function refresh() {
  updatedEl.textContent = 'Refreshing…';
  try {
    const response = await fetch('/api/dashboard', {headers: {'accept': 'application/json'}, credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const snapshot = await response.json();
    renderServices(snapshot.services);
    renderModels(snapshot.models);
    renderRoutes(snapshot.routes);
    updatedEl.textContent = `Updated ${new Date(snapshot.generated_at).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`;
  } catch (error) {
    servicesEl.innerHTML = `<div class="loading-card">Dashboard unavailable. ${escapeHtml(error.message)}</div>`;
    modelsEl.innerHTML = '<div class="empty">Retry when the console backend is ready.</div>';
    routesEl.innerHTML = '<div class="empty">Retry when the console backend is ready.</div>';
    updatedEl.textContent = 'Connection unavailable';
  }
}

document.querySelector('#refresh').addEventListener('click', refresh);
refresh();
setInterval(refresh, 30000);
