import { showError } from './ui.js';

export const uid = () =>
  Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) =>
    b.toString(16).padStart(2, '0')
  ).join('');

export const read = (key, fallback) => {
  try {
    return JSON.parse(sessionStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
};
export const write = (key, value) => {
  try {
    sessionStorage.setItem(key, JSON.stringify(value));
  } catch {
    showError(
      new Error(
        'Browser storage is full. Keep this tab open to preserve drafts and pending submissions.'
      )
    );
  }
};
export async function api(path, { responseType, ...options } = {}) {
  const headers = new Headers(options.headers);
  if (options.body instanceof Blob) {
    headers.set('Content-Type', options.body.type);
  } else if (options.body !== undefined) {
    headers.set('Content-Type', 'application/json');
    options = { ...options, body: JSON.stringify(options.body) };
  }
  if (options.method && !['GET', 'HEAD'].includes(options.method)) {
    const csrf = document.cookie
      .split('; ')
      .find((value) => value.startsWith('sola_csrf='))
      ?.split('=')[1];
    if (csrf) headers.set('X-CSRF-Token', csrf);
  }
  const response = await fetch(`/api/v1${path}`, {
    ...options,
    headers,
    credentials: 'same-origin'
  });
  options.signal?.throwIfAborted();
  if (response.status === 401) {
    document.dispatchEvent(new Event('sola-auth-required'));
    throw new Error('Your sign-in expired. Sign in to continue.');
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.error?.message || `Request failed (${response.status}).`);
  }
  if (response.status === 204) return null;
  return responseType === 'blob' ? response.blob() : response.json();
}

export async function downloadTopic(id) {
  const archive = await api(`/sessions/${id}/export`, { responseType: 'blob' });
  const url = URL.createObjectURL(archive);
  const link = document.createElement('a');
  link.href = url;
  link.download = `solakit-session-${id}.zip`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
