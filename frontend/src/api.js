async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) throw new Error(await res.text())
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  bay: () => req('/api/bay'),
  occupancies: () => req('/api/occupancies'),
  events: () => req('/api/events'),
  quarantine: () => req('/api/quarantine'),
  proofs: () => req('/api/occupancy-proofs'),
  rejects: (occupancyId) =>
    req('/api/released-rejects' + (occupancyId ? `?occupancy_id=${encodeURIComponent(occupancyId)}` : '')),
  rejectProof: (occupancyId) =>
    req(`/api/occupancies/${encodeURIComponent(occupancyId)}/reject-proof`),
  settings: () => req('/api/settings'),
  setOverdue: (overdue_hours) =>
    req('/api/settings/overdue-hours', { method: 'PUT', body: JSON.stringify({ overdue_hours }) }),
  ingest: (fixture) =>
    req('/api/ingest/run', {
      method: 'POST',
      body: JSON.stringify(fixture ? { fixture } : {}),
    }),
  writeGate: (id) => req(`/api/write-gate/${id}`),
}
