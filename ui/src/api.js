// Client for the Python backend (directorc/server.py).
// Inside Tauri the shell starts the backend on a free port and tells us where;
// in a plain browser (npm run dev) we expect `python -m directorc.server`.

const DEV_BACKEND = 'http://127.0.0.1:8765';
let base = null;

export const inTauri = () => typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;

async function resolveBase() {
  if (base) return base;
  if (inTauri()) {
    const { invoke } = await import('@tauri-apps/api/core');
    base = await invoke('backend_url');
  } else {
    base = DEV_BACKEND;
  }
  return base;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Wait for the backend to answer. Python takes a moment to start. */
export async function connect(attempts = 40) {
  const url = await resolveBase();
  for (let i = 0; i < attempts; i += 1) {
    try {
      const res = await fetch(`${url}/api/health`);
      if (res.ok) return url;
    } catch {
      // not up yet
    }
    await sleep(350);
  }
  throw new Error(`The backend at ${url} did not respond.`);
}

async function request(path, options) {
  let res;
  try {
    res = await fetch(`${base}${path}`, options);
  } catch {
    throw new Error('Lost contact with the backend.');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

const post = (path, body) =>
  request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });

const p = (name) => `/api/projects/${encodeURIComponent(name)}`;

export const api = {
  info: () => request('/api/info'),
  doctor: (project) =>
    request(`/api/doctor${project ? `?project=${encodeURIComponent(project)}` : ''}`),
  projects: () => request('/api/projects').then((d) => d.projects),
  createProject: (name) => post('/api/projects', { name }),
  project: (name) => request(p(name)),
  deleteProject: (name) => post(`${p(name)}/delete`),
  chat: (name, message, attachments) => post(`${p(name)}/chat`, { message, attachments }),
  setGate: (name, stage, approved, notes) => post(`${p(name)}/gates`, { stage, approved, notes }),
  importInbox: (name) => post(`${p(name)}/inbox`),
  open: (name, path = '', app) => post(`${p(name)}/open`, { path, app }),
  upload: (name, file, kind) =>
    request(
      `${p(name)}/references?kind=${encodeURIComponent(kind)}&name=${encodeURIComponent(file.name)}`,
      { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file },
    ).then((d) => d.reference),
  // Starts at "now"; on reconnect the browser resumes from the last event id.
  events: () => new EventSource(`${base}/api/events`),
};

/** URL of a file inside a project. `v` busts the cache when a render is redone. */
export function fileUrl(project, media) {
  const path = media.path.split('/').map(encodeURIComponent).join('/');
  return `${base}/files/${encodeURIComponent(project)}/${path}${media.v ? `?v=${media.v}` : ''}`;
}

const VIDEO = /\.(mp4|webm|mov)$/i;
const IMAGE = /\.(png|jpe?g|webp|bmp)$/i;
export const isVideo = (path) => VIDEO.test(path);
export const isImage = (path) => IMAGE.test(path);
