export function requestId(): string {
  // getRandomValues is also available when the console is served over HTTP.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
}
